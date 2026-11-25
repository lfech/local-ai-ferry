# Model selections by GPU memory

[Project overview](../README.md) | [Launcher guide](launcher.md)

Run these examples from the checkout.

The default `config/models.ini` selects SmolLM2-135M-Instruct in Q2_K,
an 88 MB chat model. [Published GGUF](https://huggingface.co/bartowski/SmolLM2-135M-Instruct-GGUF/blob/main/SmolLM2-135M-Instruct-Q2_K.gguf)

The [16 GB VRAM selection](../config/model-examples/models.16gb.ini),
[8 GB VRAM selection](../config/model-examples/models.8gb.ini) and
[6 GB VRAM selection](../config/model-examples/models.6gb.ini) are ready to use. Each
selected model is downloaded in turn.

```bash
loaf build
loaf download --models-file config/model-examples/models.16gb.ini
loaf offline
```

Edit `config/models.ini` to change the default selection. Switching
selection files changes future downloads. The models already in
the shared Ollama volume remain available. Select the configured Ollama aliases
in WebUI. The Coder entries use exact merged GGUF filename tags.
[Selection syntax](launcher.md#select-models)

## 16 GB VRAM hardware profile

| Model | Quantization | Approximate download weights | Purpose |
| --- | --- | --- | --- |
| [Qwen3-14B](https://huggingface.co/Qwen/Qwen3-14B-GGUF/tree/main) | Q5_K_M | 10.5 GB | General chat and reasoning with higher capacity |
| [Qwen2.5-Coder-14B-Instruct](https://huggingface.co/Qwen/Qwen2.5-Coder-14B-Instruct-GGUF/tree/main) | Q4_K_M | 9 GB | Coding assistant |
| [Ministral-3-3B-Reasoning-2512](https://huggingface.co/mistralai/Ministral-3-3B-Reasoning-2512-GGUF/tree/main) | Q4_K_M | 2.15 GB + 0.84 GB projector | Fast reasoning; the model already tested here |

Explicitly set **Context Length (`num_ctx`) to `16384` in WebUI**, with one active
model. Qwen3's published model parameters set `num_ctx=40960` and
`num_predict=32768`. Override those with `16384` and, initially, `4096` output
tokens for these hardware estimates. The model's own parameters can override
Ollama's server default. See the [published Qwen parameters](https://huggingface.co/Qwen/Qwen3-14B-GGUF/blob/main/params).

The models are estimated to fit in 16 GB VRAM at that context, allowing for
KV cache and compute buffers. Actual memory depends on the backend and other
GPU users. See the [GPU placement check](verification.md#debian-smoke-checks).
Keep the upstream sampling defaults, or start with temperature `0.6` for Qwen3
and `0.7` for Ministral reasoning. Temperature does not set the model's memory fit.

## 8 GB VRAM hardware profile

| Model | Quantization | Approximate download weights | Purpose |
| --- | --- | --- | --- |
| [Qwen3-4B](https://huggingface.co/Qwen/Qwen3-4B-GGUF/tree/main) | Q5_K_M | 2.89 GB | Default choice for general chat/reasoning |
| [Qwen2.5-Coder-7B-Instruct](https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF/tree/main) | Q4_K_M | 4.68 GB | Coding; the tightest memory fit in this list |
| [Ministral-3-3B-Reasoning-2512](https://huggingface.co/mistralai/Ministral-3-3B-Reasoning-2512-GGUF/tree/main) | Q4_K_M | 2.15 GB + 0.84 GB projector | Smaller, faster reasoning option |

Explicitly set **16384 context tokens** in WebUI, especially with its built-in
tools. Qwen3-4B also ships a [40960-token context preset](https://huggingface.co/Qwen/Qwen3-4B-GGUF/blob/main/params),
so leaving the control unset can use much more VRAM than these estimates. The
4B and 3B choices leave more GPU headroom. The 7B coder is an estimated fit;
desktop GPU usage and KV cache can change that. If it offloads to CPU, use one of
the smaller models, or reduce context while keeping enough room for your prompt
and answer. A 7008-token tool prompt needs considerably more than 8192 tokens
when reasoning/output is also required.

Use an up-to-date driver that supports the RTX 5060 and a current Ollama image.
[Ollama lists the RTX 5060 as supported NVIDIA hardware](https://docs.ollama.com/gpu).
The 8 GB VRAM selections are estimates.

## 6 GB VRAM hardware profile

The single-model profile selects Ministral-3B-Reasoning in Q4_K_M. In
the checkout, use `loaf download --models-file config/model.6gb.ini`.
To select it persistently, set `models_file = model.6gb.ini` under
`[launcher]` in `config/config.ini`. The launcher can also find the INI
in the packaged configuration directory.

Ministral's weights and vision projector total about 3 GB. For one
request at 16,384 tokens, the default 16-bit KV cache adds about 1.7 GB
before compute buffers. The 6 GB VRAM profile is an estimate for one
active model. [Published weights](https://huggingface.co/mistralai/Ministral-3-3B-Reasoning-2512-GGUF/tree/main) ·
[Model dimensions](https://huggingface.co/mistralai/Ministral-3-3B-Reasoning-2512/raw/main/config.json) ·
[Ollama KV cache](https://docs.ollama.com/faq#how-can-i-set-the-quantization-type-for-the-kv-cache)

## Using the hardware well

For interactive chat, keeping a model and its KV cache in VRAM is the main goal.
The CPU handles orchestration, preprocessing and any offloaded layers; RAM holds
the application and model/file caches. Fast SSDs help downloading, loading and
switching models. SSD throughput does not determine the capacity of VRAM, and
disk paging is a poor inference memory plan.

The example lists favor GPU inference so the OS and WebUI have room in system
RAM. The choices trade model capacity, quantization quality and response speed.

The download sizes cover the model files. Budget additional VRAM for the
runtime, including the KV cache and compute buffers. Under WSL2, `docker info`
reports the system memory available to the Docker Engine.
[Ollama GPU placement and context memory](https://docs.ollama.com/faq)

Hardware fits are estimates based on published sizes, architecture and a 16K
context. See the [catalog check](verification.md#check-the-model-example-specifiers)
and [recorded inference results](verification-record.md).
