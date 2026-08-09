"""Declarative llama-server parameter catalogue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    key: str
    cli: str
    support_cli: str
    label: str
    category: str
    value_type: str
    default: Any
    default_enabled: bool = False
    choices: tuple[str, ...] = ()
    min_value: int | float | None = None
    max_value: int | float | None = None
    tooltip: str = ""


KV_TYPES = ("f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1")


PARAMETER_SPECS: tuple[ParameterSpec, ...] = (
    ParameterSpec("alias", "--alias", "--alias", "Model alias", "General", "text", "", tooltip="Name exposed by the server API."),
    ParameterSpec("host", "--host", "--host", "Host", "Server / API", "text", "127.0.0.1", True, tooltip="Address to listen on. 127.0.0.1 limits access to this PC."),
    ParameterSpec("port", "--port", "--port", "Port", "Server / API", "int", 8080, True, min_value=1, max_value=65535, tooltip="TCP port used by the HTTP API (1-65535)."),
    ParameterSpec("ctx_size", "-c", "--ctx-size", "Context size", "Context / KV cache", "int", 4096, True, min_value=0, max_value=2_147_483_647, tooltip="Maximum context in tokens. Large values substantially increase KV-cache memory."),
    ParameterSpec("parallel", "-np", "--parallel", "Parallel slots", "Performance", "int", 1, True, min_value=1, max_value=1024, tooltip="Number of request slots processed in parallel; increases KV-cache use."),
    ParameterSpec("flash_attn", "--flash-attn", "--flash-attn", "Flash attention", "Context / KV cache", "choice", "auto", choices=("auto", "on", "off"), tooltip="Flash Attention mode. 'auto' lets llama.cpp choose."),
    ParameterSpec("cache_type_k", "-ctk", "--cache-type-k", "K cache type", "Context / KV cache", "choice", "f16", choices=KV_TYPES, tooltip="Key-cache data type. Quantization saves memory and may affect quality/speed."),
    ParameterSpec("cache_type_v", "-ctv", "--cache-type-v", "V cache type", "Context / KV cache", "choice", "f16", choices=KV_TYPES, tooltip="Value-cache data type. Quantization saves memory and may affect quality/speed."),
    ParameterSpec("jinja", "--jinja", "--jinja", "Jinja templates", "General", "bool", True, tooltip="Use llama.cpp's Jinja chat-template engine."),
    ParameterSpec("n_predict", "--n-predict", "--n-predict", "Max predicted tokens", "General", "int", -1, min_value=-1, max_value=2_147_483_647, tooltip="Maximum generated tokens; -1 means unlimited for the server."),
    ParameterSpec("threads", "--threads", "--threads", "Threads", "Performance", "int", -1, min_value=-1, max_value=4096, tooltip="CPU threads for token generation; -1 lets llama.cpp decide."),
    ParameterSpec("threads_batch", "--threads-batch", "--threads-batch", "Batch threads", "Performance", "int", -1, min_value=-1, max_value=4096, tooltip="CPU threads for prompt and batch processing."),
    ParameterSpec("batch_size", "--batch-size", "--batch-size", "Batch size", "Performance", "int", 2048, min_value=1, max_value=2_147_483_647, tooltip="Logical prompt-processing batch size."),
    ParameterSpec("ubatch_size", "--ubatch-size", "--ubatch-size", "Physical batch size", "Performance", "int", 512, min_value=1, max_value=2_147_483_647, tooltip="Physical micro-batch size; lower it if memory is tight."),
    ParameterSpec("priority", "--prio", "--prio", "Thread priority", "Performance", "choice", "0", choices=("-1", "0", "1", "2", "3"), tooltip="Process priority from low (-1) to realtime (3). Use realtime carefully."),
    ParameterSpec("poll", "--poll", "--poll", "Polling level", "Performance", "int", 50, min_value=0, max_value=100, tooltip="CPU polling level while waiting for work (0-100)."),
    ParameterSpec("gpu_layers", "-ngl", "--gpu-layers", "GPU layers", "Memory / GPU", "int_or_choice", "auto", choices=("auto", "all"), min_value=0, max_value=1_000_000, tooltip="Layers to offload: an integer, 'auto', or 'all'. Disabled is most portable."),
    ParameterSpec("device", "--device", "--device", "Devices", "Memory / GPU", "text", "", tooltip="Comma-separated device list; use llama-server --list-devices to inspect names."),
    ParameterSpec("main_gpu", "--main-gpu", "--main-gpu", "Main GPU", "Memory / GPU", "int", 0, min_value=0, max_value=1024, tooltip="Primary GPU index for split-mode none/row."),
    ParameterSpec("split_mode", "--split-mode", "--split-mode", "Split mode", "Memory / GPU", "choice", "layer", choices=("none", "layer", "row", "tensor"), tooltip="How weights/KV are split across multiple GPUs."),
    ParameterSpec("tensor_split", "--tensor-split", "--tensor-split", "Tensor split", "Memory / GPU", "text", "", tooltip="Per-GPU proportions, for example 3,1. Leave disabled on one GPU."),
    ParameterSpec("load_mode", "--load-mode", "--load-mode", "Load mode", "Memory / GPU", "choice", "mmap", choices=("none", "mmap", "mlock", "mmap+mlock", "dio"), tooltip="Modern model-loading mode. mmap is the normal default."),
    ParameterSpec("fit", "--fit", "--fit", "Fit to VRAM", "Memory / GPU", "choice", "on", choices=("on", "off"), tooltip="Allow current llama.cpp builds to adjust unset arguments to device memory."),
    ParameterSpec("temperature", "--temp", "--temp", "Temperature", "Sampling", "float", 0.8, False, min_value=0.0, max_value=10.0, tooltip="Controls randomness in the token distribution; higher values produce more varied output."),
    ParameterSpec("top_p", "--top-p", "--top-p", "Top P", "Sampling", "float", 0.95, False, min_value=0.0, max_value=1.0, tooltip="Limits sampling to the smallest token set whose cumulative probability reaches this value."),
    ParameterSpec("top_k", "--top-k", "--top-k", "Top K", "Sampling", "int", 40, False, min_value=0, max_value=1_000_000, tooltip="Limits sampling to the K most probable tokens; 0 disables top-k filtering."),
    ParameterSpec("min_p", "--min-p", "--min-p", "Min P", "Sampling", "float", 0.05, False, min_value=0.0, max_value=1.0, tooltip="Filters tokens below a probability threshold relative to the most likely token; 0 disables it."),
    ParameterSpec("repeat_penalty", "--repeat-penalty", "--repeat-penalty", "Repeat penalty", "Sampling", "float", 1.0, False, min_value=0.0, max_value=10.0, tooltip="Penalizes repeated tokens; 1.0 disables the penalty, while higher values discourage repetition."),
    ParameterSpec("no_mmap", "--no-mmap", "--no-mmap", "Disable mmap", "Memory / GPU", "bool", True, tooltip="Avoid memory mapping. Usually slower; deprecated in favor of --load-mode."),
    ParameterSpec("mlock", "--mlock", "--mlock", "Lock model in RAM", "Memory / GPU", "bool", True, tooltip="Prevent model memory from being swapped; may fail without enough RAM."),
    ParameterSpec("numa", "--numa", "--numa", "NUMA mode", "Memory / GPU", "choice", "distribute", choices=("distribute", "isolate", "numactl"), tooltip="NUMA policy for multi-socket systems. Leave disabled on typical PCs."),
    ParameterSpec("cpu_moe", "--cpu-moe", "--cpu-moe", "Keep all experts on CPU", "MoE", "bool", True, tooltip="Keep all MoE expert weights in system RAM. Saves VRAM but can reduce speed."),
    ParameterSpec("n_cpu_moe", "--n-cpu-moe", "--n-cpu-moe", "CPU expert layers", "MoE", "int", 0, min_value=0, max_value=1_000_000, tooltip="Keep expert weights for the first N layers on CPU. Useful for partial expert offload."),
    ParameterSpec("override_tensor", "--override-tensor", "--override-tensor", "Tensor placement override", "MoE", "text", "", tooltip="Advanced tensor-pattern=buffer overrides. Can place selected expert tensors on CPU/GPU; syntax is build-specific."),
    ParameterSpec("api_key", "--api-key", "--api-key", "API key", "Server / API", "secret", "", tooltip="Require this key for API access. Never stored in laucher-settings.json."),
    ParameterSpec("timeout", "--timeout", "--timeout", "HTTP timeout", "Server / API", "int", 3600, min_value=1, max_value=2_147_483_647, tooltip="Server read/write timeout in seconds."),
    ParameterSpec("threads_http", "--threads-http", "--threads-http", "HTTP threads", "Server / API", "int", -1, min_value=-1, max_value=4096, tooltip="Threads serving HTTP requests; -1 lets llama.cpp decide."),
    ParameterSpec("metrics", "--metrics", "--metrics", "Metrics endpoint", "Server / API", "bool", True, tooltip="Expose Prometheus-compatible metrics."),
    ParameterSpec("cache_prompt", "--cache-prompt", "--cache-prompt", "Prompt cache", "Server / API", "bool", True, tooltip="Enable reuse of prompt KV cache."),
    ParameterSpec("rope_scaling", "--rope-scaling", "--rope-scaling", "RoPE scaling", "Advanced", "choice", "linear", choices=("none", "linear", "yarn"), tooltip="Override the model's RoPE scaling method."),
    ParameterSpec("rope_scale", "--rope-scale", "--rope-scale", "RoPE scale", "Advanced", "float", 1.0, min_value=0.000001, max_value=1_000_000, tooltip="Context expansion factor. Incorrect values can degrade model output."),
    ParameterSpec("swa_full", "--swa-full", "--swa-full", "Full SWA cache", "Advanced", "bool", True, tooltip="Use a full-size sliding-window-attention cache, increasing memory use."),
    ParameterSpec("no_kv_offload", "--no-kv-offload", "--no-kv-offload", "Disable KV offload", "Advanced", "bool", True, tooltip="Keep KV-cache computation off the GPU."),
    ParameterSpec("check_tensors", "--check-tensors", "--check-tensors", "Check tensors", "Advanced", "bool", True, tooltip="Validate model tensors while loading; adds startup work."),
    ParameterSpec("log_verbosity", "--log-verbosity", "--log-verbosity", "Log verbosity", "Advanced", "int", 3, min_value=0, max_value=5, tooltip="llama.cpp log threshold: 0 generic through 5 debug."),
)


SPEC_BY_KEY = {spec.key: spec for spec in PARAMETER_SPECS}
CATEGORIES = (
    "General",
    "Sampling",
    "Memory / GPU",
    "Context / KV cache",
    "MoE",
    "Performance",
    "Server / API",
    "Advanced",
)
