import random
import sys
import typing

import numpy as np
from PIL import Image
from torch.nn import functional as F

from clip_custom import clip

sys.path.append("latent-diffusion")

import os

import cog
import torch
from torchvision import transforms
from torchvision.transforms import functional as TF
from torchvision.utils import make_grid
from tqdm.auto import tqdm

from encoders.modules import BERTEmbedder
from guided_diffusion.script_util import (
    create_gaussian_diffusion,
    create_model_and_diffusion,
    model_and_diffusion_defaults,
)


def set_requires_grad(model, value):
    for param in model.parameters():
        param.requires_grad = value


normalize = transforms.Normalize(
    mean=[0.48145466, 0.4578275, 0.40821073], std=[0.26862954, 0.26130258, 0.27577711]
)

os.environ[
    "TOKENIZERS_PARALLELISM"
] = "false"  # required to avoid errors with transformers lib


def load_aesthetic_vit_l_14_embed(
    rating: int = 9, embed_dir: str = "aesthetic-predictor/vit_l_14_embeddings"
) -> torch.Tensor:
    assert rating in [
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
    ], "rating must be in [1, 2, 3, 4, 5, 6, 7, 8, 9]"
    embed_path = os.path.join(embed_dir, f"rating{rating}.npy")
    text_emb_clip_aesthetic = np.load(embed_path)
    return torch.from_numpy(text_emb_clip_aesthetic)


def average_prompt_embed_with_aesthetic_embed(
    prompt_embed: torch.Tensor,
    aesthetic_embed: torch.Tensor,
    aesthetic_weight: float = 0.5,
) -> torch.Tensor:
    return F.normalize(
        prompt_embed * (1 - aesthetic_weight) + aesthetic_embed * aesthetic_weight
    )


def load_finetune() -> typing.Tuple[torch.nn.Module, torch.nn.Module]:
    """
    Loads the model and diffusion from an fp16 version of the model.
    """
    model_state_dict = torch.load("ongo-eval-inpaint.pt", map_location="cpu")
    model_config = model_and_diffusion_defaults()
    model_params = {
        "attention_resolutions": "32,16,8",
        "class_cond": False,
        "diffusion_steps": 1000,
        "rescale_timesteps": True,
        "timestep_respacing": "27",
        "image_size": 32,
        "learn_sigma": False,
        "noise_schedule": "linear",
        "num_channels": 320,
        "num_heads": 8,
        "num_res_blocks": 2,
        "resblock_updown": False,
        "use_fp16": True,
        "use_scale_shift_norm": False,
        "clip_embed_dim": 768 if "clip_proj.weight" in model_state_dict else None,
        "image_condition": True
        if model_state_dict["input_blocks.0.0.weight"].shape[1] == 8
        else False,
        "super_res_condition": True
        if "external_block.0.0.weight" in model_state_dict
        else False,
    }
    model_config.update(model_params)
    model, _ = create_model_and_diffusion(**model_config)
    model.load_state_dict(model_state_dict, strict=False)
    return model, model_config


class Predictor(cog.BasePredictor):
    @torch.inference_mode(mode=True)
    def setup(self):
        """Load the model into memory to make running multiple predictions efficient"""
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        torch.backends.cudnn.benchmark = True

        # Load the model and model_params
        print("Loading diffusion model")
        self.model, self.model_config = load_finetune()
        self.model.requires_grad_(False).eval().to(self.device)
        if self.model_config["use_fp16"]:
            self.model.convert_to_fp16()
        else:
            self.model.convert_to_fp32()

        # Load CLIP text encoder from slim checkpoint
        print("Loading CLIP text encoder.")
        self.clip_model, _ = clip.load("ViT-L/14", device=self.device, jit=False)
        self.clip_model.eval().requires_grad_(False)
        self.clip_model.to(self.device)
        self.clip_preprocess = normalize

        # Load VAE model
        print("Loading stage 1 VAE model")
        self.ldm = torch.load("kl-f8.pt", map_location="cpu")
        self.ldm.to(self.device)
        self.ldm.eval()
        self.ldm.requires_grad_(False)
        set_requires_grad(self.ldm, False)

        # Load BERT model
        print("Loading BERT model")
        self.bert = BERTEmbedder(1280, 32)
        bert_state_dict = torch.load("bert.pt", map_location="cpu")
        self.bert.load_state_dict(bert_state_dict)
        self.bert.half().eval()
        self.bert.to(self.device)
        set_requires_grad(self.bert, False)

    @torch.inference_mode()
    @torch.cuda.amp.autocast()
    def predict(
        self,
        prompt: str = cog.Input(description="Your text prompt.", default=""),
        aesthetic_rating: int = cog.Input(
            description="Number between 0 and 9 representing the aesthetic rating. Will initialize the prompt CLIP embed with the respective aesthetic embed.",
            default=9,
            ge=0,
            le=9,
        ),
        aesthetic_weight: float = cog.Input(
            description="Weight of the aesthetic embedding in the average prompt embedding.",
            default=0.5,
            ge=0,
            le=1,
        ),
        negative: str = cog.Input(
            default="",
            description="(optional) Negate the model's prediction for this text from the model's prediction for the target text.",
        ),
        init_image: cog.Path = cog.Input(
            default=None,
            description="(optional) Initial image to use for the model's prediction.",
        ),
        init_skip_fraction: float = cog.Input(
            default=0.0,
            description="Fraction of sampling steps to skip when using an init image.",
            ge=0.0,
            le=1.0,
        ),
        batch_size: int = cog.Input(
            default=4, description="Batch size.", choices=[1, 2, 3, 4, 6, 8]
        ),
        width: int = cog.Input(
            default=256,
            description="Target width",
            choices=[128, 192, 256, 320, 384],
        ),
        height: int = cog.Input(
            default=256,
            description="Target height",
            choices=[128, 192, 256, 320, 384],
        ),
        seed: int = cog.Input(
            default=-1,
            description="Seed for random number generator.",
            ge=-1,
            le=(2**32 - 1),
        ),
        guidance_scale: float = cog.Input(
            default=5.0,
            description="Classifier-free guidance scale. Higher values will result in more guidance toward caption, with diminishing returns. Try values between 1.0 and 40.0.",
            le=100.0,
            ge=-20.0,
        ),
        steps: int = cog.Input(
            default=150,
            description="Number of diffusion steps to run.",
            le=250,
            ge=15,
        ),
    ) -> typing.Iterator[cog.Path]:
        if seed == -1:
            seed = random.randint(0, 2**32 - 1)
        torch.manual_seed(seed)
        # Create diffusion manually so we don't re-init the model just to change timestep_respacing
        self.model_config["timestep_respacing"] = str(steps)
        self.diffusion = create_gaussian_diffusion(
            steps=self.model_config["diffusion_steps"],
            learn_sigma=self.model_config["learn_sigma"],
            noise_schedule=self.model_config["noise_schedule"],
            use_kl=self.model_config["use_kl"],
            predict_xstart=self.model_config["predict_xstart"],
            rescale_timesteps=self.model_config["rescale_timesteps"],
            timestep_respacing=self.model_config["timestep_respacing"],
        )

        # Bert context
        print("Encoding text with BERT")
        text_emb = self.bert.encode([prompt] * batch_size).to(self.device).float()
        text_blank = self.bert.encode([negative] * batch_size).to(self.device).float()

        # CLIP context
        print("Encoding text with CLIP")
        text_tokens_clip = clip.tokenize([prompt] * batch_size, truncate=True).to(
            self.device
        )
        text_tokens_clip_blank = clip.tokenize(
            [negative] * batch_size, truncate=True
        ).to(self.device)

        text_emb_clip = self.clip_model.encode_text(text_tokens_clip)
        text_emb_clip_blank = self.clip_model.encode_text(text_tokens_clip_blank)

        print(
            f"Using aesthetic embedding {aesthetic_rating} with weight {aesthetic_weight}"
        )
        text_emb_clip_aesthetic = load_aesthetic_vit_l_14_embed(
            aesthetic_rating, "aesthetic-predictor/vit_l_14_embeddings"
        ).to(self.device)
        text_emb_clip = average_prompt_embed_with_aesthetic_embed(
            text_emb_clip, text_emb_clip_aesthetic, aesthetic_weight
        )

        # since we arent using a mask or editing an image, we set the image_embed to zeros
        image_embed = torch.zeros(
            batch_size * 2, 4, height // 8, width // 8, device=self.device
        )
        print("Packing CLIP and BERT embeddings into kwargs")
        kwargs = {
            "context": torch.cat([text_emb, text_blank], dim=0).half(),
            "clip_embed": torch.cat([text_emb_clip, text_emb_clip_blank], dim=0).half()
            if self.model_config["clip_embed_dim"]
            else None,
            "image_embed": image_embed,
        }

        # Create a classifier-free guidance sampling function
        def model_fn(x_t, ts, **kwargs):
            half = x_t[: len(x_t) // 2]
            combined = torch.cat([half, half], dim=0)
            model_out = self.model(combined, ts, **kwargs)
            eps, rest = model_out[:, :3], model_out[:, 3:]
            cond_eps, uncond_eps = torch.split(eps, len(eps) // 2, dim=0)
            half_eps = uncond_eps + guidance_scale * (cond_eps - uncond_eps)
            eps = torch.cat([half_eps, half_eps], dim=0)
            return torch.cat([eps, rest], dim=1)

        images_per_row = batch_size
        if batch_size >= 6:
            images_per_row = batch_size // 2

        def save_sample(sample):
            final_outputs = []
            for image in sample["pred_xstart"][:batch_size]:
                image /= 0.18215
                im = image.unsqueeze(0)
                out = self.ldm.decode(im)
                final_outputs.append(out.squeeze(0).add(1).div(2).clamp(0, 1))
            grid = make_grid(final_outputs, nrow=images_per_row)
            return grid

        if init_image:
            if init_skip_fraction == 0.0:
                print(f"Must specify init_skip_fraction > 0.0 when using init_image.")
                print(f"Overriding init_skip_fraction to 0.5")
                init_skip_fraction = 0.5
            print(
                f"Loading initial image {init_image} with init_skip_fraction: {init_skip_fraction}"
            )
            init = Image.open(init_image).convert("RGB")
            init = init.resize((int(width), int(height)), Image.LANCZOS)
            init = TF.to_tensor(init).to(self.device).unsqueeze(0).clamp(0, 1)
            h = self.ldm.encode(init * 2 - 1).sample() * 0.18215
            init = torch.cat(batch_size * 2 * [h], dim=0)
            # str to int * float -> float
            init_skip_timesteps = (
                int(self.model_config["timestep_respacing"]) * init_skip_fraction
            )
            # float to int
            init_skip_timesteps = int(init_skip_timesteps)
        else:
            init = None
            init_skip_fraction = 0.0
            init_skip_timesteps = 0

        sample_fn = self.diffusion.plms_sample_loop_progressive
        samples = sample_fn(
            model_fn,
            (batch_size * 2, 4, int(height / 8), int(width / 8)),
            clip_denoised=False,
            model_kwargs=kwargs,
            cond_fn=None,
            device=self.device,
            progress=True,
            init_image=init,
            skip_timesteps=init_skip_timesteps,
        )

        print("Running diffusion...")
        for j, sample in tqdm(enumerate(samples)):
            if j % 1 == 0:
                current_output = save_sample(sample)
                TF.to_pil_image(current_output).save("current.png")
                yield cog.Path("current.png")

        print(f"Finished generating with seed {seed}")