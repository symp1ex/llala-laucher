"""Declarative catalogue for llama.cpp b10427 llama-server parameters."""

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
    aliases: tuple[str, ...] = ()
    negative_cli: str | None = None
    negative_aliases: tuple[str, ...] = ()
    arity: int = 1
    repeatable: bool = False

    @property
    def positive_switches(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.cli, self.support_cli, *self.aliases)))

    @property
    def negative_switches(self) -> tuple[str, ...]:
        values = (() if self.negative_cli is None else (self.negative_cli,)) + self.negative_aliases
        return tuple(dict.fromkeys(values))

    @property
    def all_switches(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.positive_switches, *self.negative_switches)))


KV_TYPES = ("f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1")
ON_OFF_AUTO = ("auto", "on", "off")
ON_OFF = ("on", "off")
_UI_CATEGORY_MERGES = {"Security / Tools": "Server / API", "Logging": "Advanced"}


def P(key: str, cli: str, label: str, category: str, value_type: str = "text", default: Any = "", *, support: str | None = None, enabled: bool = False, choices: tuple[str, ...] = (), minimum: int | float | None = None, maximum: int | float | None = None, tooltip: str = "", aliases: tuple[str, ...] = (), negative: str | None = None, negative_aliases: tuple[str, ...] = (), arity: int = 1, repeatable: bool = False) -> ParameterSpec:
    ui_category = _UI_CATEGORY_MERGES.get(category, category)
    return ParameterSpec(key, cli, support or cli, label, ui_category, value_type, default, enabled, choices, minimum, maximum, tooltip or label, aliases, negative, negative_aliases, arity, repeatable)


def B(key: str, cli: str, label: str, category: str, **kwargs: Any) -> ParameterSpec:
    return P(key, cli, label, category, "bool", True, **kwargs)


def I(key: str, cli: str, label: str, category: str, default: int, **kwargs: Any) -> ParameterSpec:
    return P(key, cli, label, category, "int", default, **kwargs)


def F(key: str, cli: str, label: str, category: str, default: float, **kwargs: Any) -> ParameterSpec:
    return P(key, cli, label, category, "float", default, **kwargs)


def C(key: str, cli: str, label: str, category: str, default: str, choices: tuple[str, ...], **kwargs: Any) -> ParameterSpec:
    return P(key, cli, label, category, "choice", default, choices=choices, **kwargs)


def G(key: str, cli: str, negative: str, label: str, category: str, default: str = "on", **kwargs: Any) -> ParameterSpec:
    return P(key, cli, label, category, "toggle", default, choices=ON_OFF, negative=negative, **kwargs)


# Every b10427 configuration declaration has one entry below. The only
# exceptions are documented in internal.cli_inventory.
_ALL_PARAMETER_SPECS: tuple[ParameterSpec, ...] = (
    # General
    P("alias", "--alias", "Model alias", "General", aliases=("-a",), tooltip="Comma-separated names exposed by the API."),
    I("n_predict", "--n-predict", "Max predicted tokens", "General", -1, aliases=("-n", "--predict"), minimum=-1),
    I("keep", "--keep", "Prompt tokens to keep", "General", 0, minimum=-1),
    G("escape", "--escape", "--no-escape", "Process escape sequences", "General", aliases=("-e",)),
    B("special", "--special", "Output special tokens", "General", aliases=("-sp",)),
    B("spm_infill", "--spm-infill", "SPM infill pattern", "General"),
    P("reverse_prompt", "--reverse-prompt", "Reverse prompts", "General", "string_list", [], aliases=("-r",), repeatable=True, tooltip="JSON array; repeats --reverse-prompt once per item."),

    # CPU and execution
    I("threads", "--threads", "Threads", "Performance", -1, aliases=("-t",), minimum=-1, maximum=4096),
    I("threads_batch", "--threads-batch", "Batch threads", "Performance", -1, aliases=("-tb",), minimum=-1, maximum=4096),
    P("cpu_mask", "--cpu-mask", "CPU affinity mask", "Performance", aliases=("-C",)),
    P("cpu_range", "--cpu-range", "CPU affinity range", "Performance", aliases=("-Cr",)),
    C("cpu_strict", "--cpu-strict", "Strict CPU placement", "Performance", "0", ("0", "1")),
    C("priority", "--prio", "Thread priority", "Performance", "0", ("-1", "0", "1", "2", "3")),
    I("poll", "--poll", "Polling level", "Performance", 50, minimum=0, maximum=100),
    P("cpu_mask_batch", "--cpu-mask-batch", "Batch CPU mask", "Performance", aliases=("-Cb",)),
    P("cpu_range_batch", "--cpu-range-batch", "Batch CPU range", "Performance", aliases=("-Crb",)),
    C("cpu_strict_batch", "--cpu-strict-batch", "Strict batch placement", "Performance", "0", ("0", "1")),
    C("priority_batch", "--prio-batch", "Batch priority", "Performance", "0", ("0", "1", "2", "3")),
    C("poll_batch", "--poll-batch", "Batch polling", "Performance", "1", ("0", "1")),
    I("batch_size", "--batch-size", "Batch size", "Performance", 2048, aliases=("-b",), minimum=1),
    I("ubatch_size", "--ubatch-size", "Physical batch size", "Performance", 512, aliases=("-ub",), minimum=1),
    G("perf", "--perf", "--no-perf", "Performance timings", "Performance", default="off"),
    G("repack", "--repack", "--no-repack", "Weight repacking", "Performance", negative_aliases=("-nr",)),
    G("op_offload", "--op-offload", "--no-op-offload", "Offload tensor operations", "Performance"),
    G("warmup", "--warmup", "--no-warmup", "Model warmup", "Performance"),

    # Context and KV cache
    I("ctx_size", "-c", "Context size", "Context / KV cache", 4096, support="--ctx-size", aliases=("--ctx-size",), enabled=True, minimum=0),
    B("swa_full", "--swa-full", "Full SWA cache", "Context / KV cache"),
    C("flash_attn", "--flash-attn", "Flash attention", "Context / KV cache", "auto", ON_OFF_AUTO, aliases=("-fa",)),
    C("rope_scaling", "--rope-scaling", "RoPE scaling", "Context / KV cache", "linear", ("none", "linear", "yarn")),
    F("rope_scale", "--rope-scale", "RoPE scale", "Context / KV cache", 1.0, minimum=0.000001),
    F("rope_freq_base", "--rope-freq-base", "RoPE base frequency", "Context / KV cache", 0.0, minimum=0.0),
    F("rope_freq_scale", "--rope-freq-scale", "RoPE frequency scale", "Context / KV cache", 1.0, minimum=0.0),
    I("yarn_orig_ctx", "--yarn-orig-ctx", "YaRN original context", "Context / KV cache", 0, minimum=0),
    F("yarn_ext_factor", "--yarn-ext-factor", "YaRN extrapolation factor", "Context / KV cache", -1.0),
    F("yarn_attn_factor", "--yarn-attn-factor", "YaRN attention factor", "Context / KV cache", -1.0),
    F("yarn_beta_slow", "--yarn-beta-slow", "YaRN beta slow", "Context / KV cache", -1.0),
    F("yarn_beta_fast", "--yarn-beta-fast", "YaRN beta fast", "Context / KV cache", -1.0),
    G("no_kv_offload", "--no-kv-offload", "--kv-offload", "Disable KV offload", "Context / KV cache", aliases=("-nkvo",), negative_aliases=("-kvo",)),
    C("cache_type_k", "-ctk", "K cache type", "Context / KV cache", "f16", KV_TYPES, support="--cache-type-k", aliases=("--cache-type-k",)),
    C("cache_type_v", "-ctv", "V cache type", "Context / KV cache", "f16", KV_TYPES, support="--cache-type-v", aliases=("--cache-type-v",)),
    F("defrag_thold", "--defrag-thold", "KV defrag threshold", "Context / KV cache", -1.0, aliases=("-dt",)),
    I("ctx_checkpoints", "--ctx-checkpoints", "Context checkpoints", "Context / KV cache", 0, aliases=("-ctxcp", "--swa-checkpoints"), minimum=0),
    I("checkpoint_min_step", "--checkpoint-min-step", "Checkpoint minimum step", "Context / KV cache", 8192, aliases=("-cms",), minimum=0),
    I("cache_ram", "--cache-ram", "RAM cache limit MiB", "Context / KV cache", 8192, aliases=("-cram",), minimum=-1),
    G("kv_unified", "--kv-unified", "--no-kv-unified", "Unified KV cache", "Context / KV cache", aliases=("-kvu",), negative_aliases=("-no-kvu",)),
    G("cache_idle_slots", "--cache-idle-slots", "--no-cache-idle-slots", "Cache idle slots", "Context / KV cache"),
    G("context_shift", "--context-shift", "--no-context-shift", "Context shifting", "Context / KV cache"),
    C("pooling", "--pooling", "Embedding pooling", "Context / KV cache", "none", ("none", "mean", "cls", "last", "rank")),

    # Memory, devices, and loading
    B("no_host", "--no-host", "Bypass host buffer", "Memory / GPU"),
    P("rpc", "--rpc", "RPC servers", "Memory / GPU"),
    B("mlock", "--mlock", "Lock model in RAM", "Memory / GPU"),
    G("no_mmap", "--no-mmap", "--mmap", "Disable mmap", "Memory / GPU"),
    G("direct_io", "--direct-io", "--no-direct-io", "Direct I/O", "Memory / GPU", aliases=("-dio",), negative_aliases=("-ndio",)),
    C("load_mode", "--load-mode", "Load mode", "Memory / GPU", "mmap", ("auto", "none", "mmap", "mlock", "mmap+mlock", "dio"), aliases=("-lm",)),
    C("numa", "--numa", "NUMA mode", "Memory / GPU", "distribute", ("distribute", "isolate", "numactl")),
    P("device", "--device", "Devices", "Memory / GPU", aliases=("-dev",)),
    P("override_tensor", "--override-tensor", "Tensor placement override", "Memory / GPU", aliases=("-ot",)),
    P("gpu_layers", "-ngl", "GPU layers", "Memory / GPU", "int_or_choice", "auto", support="--gpu-layers", aliases=("--gpu-layers", "--n-gpu-layers"), choices=("auto", "all"), minimum=0),
    C("split_mode", "--split-mode", "Split mode", "Memory / GPU", "layer", ("none", "layer", "row", "tensor"), aliases=("-sm",)),
    P("tensor_split", "--tensor-split", "Tensor split", "Memory / GPU", aliases=("-ts",)),
    I("main_gpu", "--main-gpu", "Main GPU", "Memory / GPU", 0, aliases=("-mg",), minimum=0),
    C("fit", "--fit", "Fit to VRAM", "Memory / GPU", "on", ON_OFF, aliases=("-fit",)),
    P("fit_target", "--fit-target", "Fit target margins", "Memory / GPU", default="1024", aliases=("-fitt",)),
    I("fit_ctx", "--fit-ctx", "Minimum fit context", "Memory / GPU", 4096, aliases=("-fitc",), minimum=0),
    B("check_tensors", "--check-tensors", "Check tensors", "Memory / GPU"),
    P("override_kv", "--override-kv", "Model metadata overrides", "Memory / GPU"),
    B("cpu_moe", "--cpu-moe", "Keep all experts on CPU", "MoE", aliases=("-cmoe",)),
    I("n_cpu_moe", "--n-cpu-moe", "CPU expert layers", "MoE", 0, aliases=("-ncmoe",), minimum=0),

    # Models and adapters (-m/--model is launcher-managed)
    P("model_url", "--model-url", "Model download URL", "Models / Adapters", aliases=("-mu",)),
    P("docker_repo", "--docker-repo", "Docker model repository", "Models / Adapters", aliases=("-dr",)),
    P("hf_repo", "--hf-repo", "Hugging Face repository", "Models / Adapters", aliases=("-hf", "-hfr")),
    P("hf_file", "--hf-file", "Hugging Face file", "Models / Adapters", aliases=("-hff",)),
    P("hf_token", "--hf-token", "Hugging Face token", "Models / Adapters", "secret", "", aliases=("-hft",)),
    P("lora", "--lora", "LoRA adapters", "Models / Adapters"),
    P("lora_scaled", "--lora-scaled", "Scaled LoRA adapters", "Models / Adapters"),
    P("control_vector", "--control-vector", "Control vectors", "Models / Adapters"),
    P("control_vector_scaled", "--control-vector-scaled", "Scaled control vectors", "Models / Adapters"),
    P("control_vector_layer_range", "--control-vector-layer-range", "Control-vector layer range", "Models / Adapters", "int_list", [0, 0], arity=2, tooltip="JSON array [START, END], emitted as two argv values."),
    B("lora_init_without_apply", "--lora-init-without-apply", "Load LoRA without applying", "Models / Adapters"),

    # Sampling
    P("samplers", "--samplers", "Sampler pipeline", "Sampling", default="penalties;dry;top_n_sigma;top_k;typ_p;top_p;min_p;xtc;temperature"),
    I("seed", "--seed", "Random seed", "Sampling", -1, aliases=("-s",), minimum=-1),
    P("sampling_seq", "--sampling-seq", "Sampler sequence", "Sampling", default="edskypmxt", aliases=("--sampler-seq",)),
    B("ignore_eos", "--ignore-eos", "Ignore EOS", "Sampling"),
    F("temperature", "--temp", "Temperature", "Sampling", 0.8, aliases=("--temperature",), minimum=0.0, maximum=10.0),
    I("top_k", "--top-k", "Top K", "Sampling", 40, minimum=0, maximum=1_000_000),
    F("top_p", "--top-p", "Top P", "Sampling", 0.95, minimum=0.0, maximum=1.0),
    F("min_p", "--min-p", "Min P", "Sampling", 0.05, minimum=0.0, maximum=1.0),
    F("top_n_sigma", "--top-n-sigma", "Top N sigma", "Sampling", -1.0, aliases=("--top-nsigma",)),
    F("xtc_probability", "--xtc-probability", "XTC probability", "Sampling", 0.0, minimum=0.0, maximum=1.0),
    F("xtc_threshold", "--xtc-threshold", "XTC threshold", "Sampling", 0.1, minimum=0.0, maximum=1.0),
    F("typical_p", "--typical-p", "Typical P", "Sampling", 1.0, aliases=("--typical",), minimum=0.0, maximum=1.0),
    I("repeat_last_n", "--repeat-last-n", "Repeat lookback", "Sampling", 64, minimum=0),
    F("repeat_penalty", "--repeat-penalty", "Repeat penalty", "Sampling", 1.0, minimum=0.0, maximum=10.0),
    F("presence_penalty", "--presence-penalty", "Presence penalty", "Sampling", 0.0),
    F("frequency_penalty", "--frequency-penalty", "Frequency penalty", "Sampling", 0.0),
    F("dry_multiplier", "--dry-multiplier", "DRY multiplier", "Sampling", 0.0, minimum=0.0),
    F("dry_base", "--dry-base", "DRY base", "Sampling", 1.75, minimum=0.0),
    I("dry_allowed_length", "--dry-allowed-length", "DRY allowed length", "Sampling", 2, minimum=0),
    I("dry_penalty_last_n", "--dry-penalty-last-n", "DRY lookback", "Sampling", 64, minimum=0),
    P("dry_sequence_breaker", "--dry-sequence-breaker", "DRY sequence breakers", "Sampling", "string_list", [], repeatable=True, tooltip="JSON array; repeats the option per item."),
    F("adaptive_target", "--adaptive-target", "Adaptive-P target", "Sampling", -1.0, minimum=-1.0, maximum=1.0),
    F("adaptive_decay", "--adaptive-decay", "Adaptive-P decay", "Sampling", 0.9, minimum=0.0, maximum=0.99),
    F("dynatemp_range", "--dynatemp-range", "Dynamic temperature range", "Sampling", 0.0, minimum=0.0),
    F("dynatemp_exp", "--dynatemp-exp", "Dynamic temperature exponent", "Sampling", 1.0),
    C("mirostat", "--mirostat", "Mirostat mode", "Sampling", "0", ("0", "1", "2")),
    F("mirostat_lr", "--mirostat-lr", "Mirostat learning rate", "Sampling", 0.1, minimum=0.0),
    F("mirostat_ent", "--mirostat-ent", "Mirostat target entropy", "Sampling", 5.0, minimum=0.0),
    P("logit_bias", "--logit-bias", "Logit biases", "Sampling", "string_list", [], aliases=("-l",), repeatable=True, tooltip="JSON array such as [\"15043+1\", \"15044-1\"]."),
    P("grammar", "--grammar", "Grammar", "Sampling"),
    P("grammar_file", "--grammar-file", "Grammar file", "Sampling"),
    P("json_schema", "--json-schema", "JSON schema", "Sampling", aliases=("-j",)),
    P("json_schema_file", "--json-schema-file", "JSON schema file", "Sampling", aliases=("-jf",)),
    B("backend_sampling", "--backend-sampling", "Backend sampling", "Sampling", aliases=("-bs",)),

    # Speculative decoding
    P("spec_draft_hf", "--hf-repo-draft", "Draft HF repository", "Speculative", aliases=("--spec-draft-hf", "-hfd", "-hfrd")),
    I("spec_draft_threads", "--threads-draft", "Draft threads", "Speculative", -1, aliases=("--spec-draft-threads", "-td"), minimum=-1),
    I("spec_draft_threads_batch", "--threads-batch-draft", "Draft batch threads", "Speculative", -1, aliases=("--spec-draft-threads-batch", "-tbd"), minimum=-1),
    P("spec_draft_cpu_mask", "--cpu-mask-draft", "Draft CPU mask", "Speculative", aliases=("--spec-draft-cpu-mask", "-Cd")),
    P("spec_draft_cpu_range", "--cpu-range-draft", "Draft CPU range", "Speculative", aliases=("--spec-draft-cpu-range", "-Crd")),
    C("spec_draft_cpu_strict", "--cpu-strict-draft", "Strict draft CPU", "Speculative", "0", ("0", "1"), aliases=("--spec-draft-cpu-strict",)),
    C("spec_draft_prio", "--prio-draft", "Draft priority", "Speculative", "0", ("0", "1", "2", "3"), aliases=("--spec-draft-prio",)),
    C("spec_draft_poll", "--poll-draft", "Draft polling", "Speculative", "1", ("0", "1"), aliases=("--spec-draft-poll",)),
    P("spec_draft_cpu_mask_batch", "--cpu-mask-batch-draft", "Draft batch CPU mask", "Speculative", aliases=("--spec-draft-cpu-mask-batch", "-Cbd")),
    C("spec_draft_cpu_strict_batch", "--cpu-strict-batch-draft", "Strict draft batch CPU", "Speculative", "0", ("0", "1"), aliases=("--spec-draft-cpu-strict-batch",)),
    C("spec_draft_prio_batch", "--prio-batch-draft", "Draft batch priority", "Speculative", "0", ("0", "1", "2", "3"), aliases=("--spec-draft-prio-batch",)),
    C("spec_draft_poll_batch", "--poll-batch-draft", "Draft batch polling", "Speculative", "1", ("0", "1"), aliases=("--spec-draft-poll-batch",)),
    P("spec_draft_override_tensor", "--override-tensor-draft", "Draft tensor override", "Speculative", aliases=("--spec-draft-override-tensor", "-otd")),
    B("spec_draft_cpu_moe", "--cpu-moe-draft", "Draft experts on CPU", "Speculative", aliases=("--spec-draft-cpu-moe", "-cmoed")),
    I("spec_draft_n_cpu_moe", "--n-cpu-moe-draft", "Draft CPU expert layers", "Speculative", 0, aliases=("--spec-draft-n-cpu-moe", "--spec-draft-ncmoe", "-ncmoed"), minimum=0),
    I("spec_draft_n_max", "--spec-draft-n-max", "Maximum draft tokens", "Speculative", 3, minimum=0),
    I("spec_draft_n_min", "--spec-draft-n-min", "Minimum draft tokens", "Speculative", 0, minimum=0),
    F("spec_draft_p_split", "--spec-draft-p-split", "Draft split probability", "Speculative", 0.1, aliases=("--draft-p-split",), minimum=0.0, maximum=1.0),
    F("spec_draft_p_min", "--spec-draft-p-min", "Draft minimum probability", "Speculative", 0.0, aliases=("--draft-p-min",), minimum=0.0, maximum=1.0),
    G("spec_draft_backend_sampling", "--spec-draft-backend-sampling", "--no-spec-draft-backend-sampling", "Draft backend sampling", "Speculative"),
    P("spec_draft_device", "--device-draft", "Draft devices", "Speculative", aliases=("--spec-draft-device", "-devd")),
    P("spec_draft_gpu_layers", "--gpu-layers-draft", "Draft GPU layers", "Speculative", "int_or_choice", "auto", aliases=("--spec-draft-ngl", "-ngld", "--n-gpu-layers-draft"), choices=("auto", "all"), minimum=0),
    P("spec_draft_model", "--model-draft", "Draft model", "Speculative", aliases=("--spec-draft-model", "-md")),
    P("spec_type", "--spec-type", "Speculative methods", "Speculative", default="none"),
    I("spec_ngram_mod_n_min", "--spec-ngram-mod-n-min", "N-gram mod minimum", "Speculative", 48, minimum=1),
    I("spec_ngram_mod_n_max", "--spec-ngram-mod-n-max", "N-gram mod maximum", "Speculative", 64, minimum=1),
    I("spec_ngram_mod_n_match", "--spec-ngram-mod-n-match", "N-gram mod match", "Speculative", 24, minimum=1),
    I("spec_ngram_simple_size_n", "--spec-ngram-simple-size-n", "Simple N-gram lookup", "Speculative", 12, minimum=1),
    I("spec_ngram_simple_size_m", "--spec-ngram-simple-size-m", "Simple N-gram draft", "Speculative", 48, minimum=1),
    I("spec_ngram_simple_min_hits", "--spec-ngram-simple-min-hits", "Simple N-gram hits", "Speculative", 1, minimum=1),
    I("spec_ngram_map_k_size_n", "--spec-ngram-map-k-size-n", "Map-K lookup", "Speculative", 12, minimum=1),
    I("spec_ngram_map_k_size_m", "--spec-ngram-map-k-size-m", "Map-K draft", "Speculative", 48, minimum=1),
    I("spec_ngram_map_k_min_hits", "--spec-ngram-map-k-min-hits", "Map-K hits", "Speculative", 1, minimum=1),
    I("spec_ngram_map_k4v_size_n", "--spec-ngram-map-k4v-size-n", "Map-K4V lookup", "Speculative", 12, minimum=1),
    I("spec_ngram_map_k4v_size_m", "--spec-ngram-map-k4v-size-m", "Map-K4V draft", "Speculative", 48, minimum=1),
    I("spec_ngram_map_k4v_min_hits", "--spec-ngram-map-k4v-min-hits", "Map-K4V hits", "Speculative", 1, minimum=1),
    P("lookup_cache_static", "--lookup-cache-static", "Static lookup cache", "Speculative", aliases=("-lcs",)),
    P("lookup_cache_dynamic", "--lookup-cache-dynamic", "Dynamic lookup cache", "Speculative", aliases=("-lcd",)),
    C("spec_draft_cache_type_k", "--cache-type-k-draft", "Draft K cache type", "Speculative", "f16", KV_TYPES, aliases=("--spec-draft-type-k", "-ctkd")),
    C("spec_draft_cache_type_v", "--cache-type-v-draft", "Draft V cache type", "Speculative", "f16", KV_TYPES, aliases=("--spec-draft-type-v", "-ctvd")),
    B("spec_default", "--spec-default", "Default speculative configuration", "Speculative"),

    # Multimodal
    P("mmproj", "--mmproj", "Multimodal projector", "Multimodal", aliases=("-mm",)),
    P("mmproj_url", "--mmproj-url", "Projector download URL", "Multimodal", aliases=("-mmu",)),
    G("mmproj_auto", "--mmproj-auto", "--no-mmproj-auto", "Automatic projector", "Multimodal", negative_aliases=("--no-mmproj",)),
    G("mmproj_offload", "--mmproj-offload", "--no-mmproj-offload", "Projector GPU offload", "Multimodal"),
    I("image_min_tokens", "--image-min-tokens", "Minimum image tokens", "Multimodal", 0, minimum=0),
    I("image_max_tokens", "--image-max-tokens", "Maximum image tokens", "Multimodal", 0, minimum=0),
    I("mtmd_batch_max_tokens", "--mtmd-batch-max-tokens", "Multimodal batch tokens", "Multimodal", 0, minimum=0),

    # HTTP server, endpoints, and routing
    I("parallel", "-np", "Parallel slots", "Server / API", 1, support="--parallel", aliases=("--parallel",), enabled=True, minimum=1, maximum=1024),
    G("cont_batching", "--cont-batching", "--no-cont-batching", "Continuous batching", "Server / API", aliases=("-cb",), negative_aliases=("-nocb",)),
    P("tags", "--tags", "Model tags", "Server / API"),
    I("embd_normalize", "--embd-normalize", "Embedding normalization", "Server / API", 2, minimum=-1),
    P("host", "--host", "Host", "Server / API", default="127.0.0.1", enabled=True),
    I("port", "--port", "Port", "Server / API", 8080, enabled=True, minimum=1, maximum=65535),
    B("reuse_port", "--reuse-port", "Reuse listening port", "Server / API"),
    P("static_path", "--path", "Static files path", "Server / API"),
    P("cors_origins", "--cors-origins", "CORS origins", "Server / API", default="*"),
    P("cors_methods", "--cors-methods", "CORS methods", "Server / API", default="GET,POST,DELETE,OPTIONS"),
    P("cors_headers", "--cors-headers", "CORS headers", "Server / API", default="*"),
    G("cors_credentials", "--cors-credentials", "--no-cors-credentials", "CORS credentials", "Server / API"),
    P("api_prefix", "--api-prefix", "API path prefix", "Server / API"),
    P("ui_config", "--ui-config", "Web UI configuration JSON", "Server / API", aliases=("--webui-config",)),
    P("ui_config_file", "--ui-config-file", "Web UI configuration file", "Server / API", aliases=("--webui-config-file",)),
    G("ui", "--ui", "--no-ui", "Web UI", "Server / API", aliases=("--webui",), negative_aliases=("--no-webui",)),
    B("embedding", "--embedding", "Embeddings-only mode", "Server / API", aliases=("--embeddings",)),
    B("rerank", "--rerank", "Reranking endpoint", "Server / API", aliases=("--reranking",)),
    I("timeout", "--timeout", "HTTP timeout", "Server / API", 3600, aliases=("-to",), minimum=1),
    I("sse_ping_interval", "--sse-ping-interval", "SSE ping interval", "Server / API", 30, minimum=-1),
    I("threads_http", "--threads-http", "HTTP threads", "Server / API", -1, minimum=-1),
    G("cache_prompt", "--cache-prompt", "--no-cache-prompt", "Prompt cache", "Server / API"),
    I("cache_reuse", "--cache-reuse", "Prompt cache reuse", "Server / API", 0, minimum=0),
    B("metrics", "--metrics", "Metrics endpoint", "Server / API"),
    B("props", "--props", "Mutable properties endpoint", "Server / API"),
    G("slots", "--slots", "--no-slots", "Slots endpoint", "Server / API"),
    P("slot_save_path", "--slot-save-path", "Slot cache path", "Server / API"),
    P("media_path", "--media-path", "Local media path", "Server / API"),
    P("models_dir", "--models-dir", "Router models directory", "Server / API"),
    P("models_preset", "--models-preset", "Router presets INI", "Server / API"),
    I("models_max", "--models-max", "Maximum loaded router models", "Server / API", 4, minimum=0),
    G("models_autoload", "--models-autoload", "--no-models-autoload", "Router model autoload", "Server / API"),
    F("slot_prompt_similarity", "--slot-prompt-similarity", "Slot prompt similarity", "Server / API", 0.1, aliases=("-sps",), minimum=0.0, maximum=1.0),
    I("sleep_idle_seconds", "--sleep-idle-seconds", "Sleep after idle seconds", "Server / API", -1, minimum=-1),

    # Security, tools, and MCP (--mcp-servers-json is Web Search managed)
    G("ui_mcp_proxy", "--ui-mcp-proxy", "--no-ui-mcp-proxy", "Web UI MCP proxy", "Security / Tools", aliases=("--webui-mcp-proxy",), negative_aliases=("--no-webui-mcp-proxy",)),
    P("tools", "--tools", "Built-in tools", "Security / Tools"),
    P("tools_runtime", "--tools-runtime", "Tool runtime", "Security / Tools"),
    P("mcp_servers_config", "--mcp-servers-config", "MCP servers config file", "Security / Tools"),
    G("agent", "--agent", "--no-agent", "Agent mode", "Security / Tools", aliases=("-ag",), negative_aliases=("-no-ag",)),
    P("api_key", "--api-key", "API key", "Security / Tools", "secret", ""),
    P("api_key_file", "--api-key-file", "API key file", "Security / Tools"),
    P("ssl_key_file", "--ssl-key-file", "TLS private key file", "Security / Tools"),
    P("ssl_cert_file", "--ssl-cert-file", "TLS certificate file", "Security / Tools"),

    # Templates and reasoning
    P("chat_template_kwargs", "--chat-template-kwargs", "Template arguments JSON", "Templates / Reasoning"),
    G("jinja", "--jinja", "--no-jinja", "Jinja templates", "Templates / Reasoning"),
    C("reasoning_format", "--reasoning-format", "Reasoning format", "Templates / Reasoning", "auto", ("auto", "none", "deepseek", "deepseek-legacy")),
    C("reasoning", "--reasoning", "Reasoning mode", "Templates / Reasoning", "auto", ON_OFF_AUTO, aliases=("-rea",)),
    I("reasoning_budget", "--reasoning-budget", "Reasoning token budget", "Templates / Reasoning", -1, minimum=-1),
    P("reasoning_budget_message", "--reasoning-budget-message", "Reasoning budget message", "Templates / Reasoning"),
    G("reasoning_preserve", "--reasoning-preserve", "--no-reasoning-preserve", "Preserve reasoning history", "Templates / Reasoning"),
    P("chat_template", "--chat-template", "Chat template", "Templates / Reasoning"),
    P("chat_template_file", "--chat-template-file", "Chat template file", "Templates / Reasoning"),
    G("skip_chat_parsing", "--skip-chat-parsing", "--no-skip-chat-parsing", "Skip chat parsing", "Templates / Reasoning"),
    G("prefill_assistant", "--prefill-assistant", "--no-prefill-assistant", "Assistant prefill behavior", "Templates / Reasoning"),

    # Logging
    B("log_disable", "--log-disable", "Disable logging", "Logging"),
    P("log_file", "--log-file", "Log file", "Logging"),
    C("log_colors", "--log-colors", "Log colors", "Logging", "auto", ON_OFF_AUTO),
    B("log_verbose", "--log-verbose", "Maximum verbosity", "Logging", aliases=("-v", "--verbose")),
    B("offline", "--offline", "Offline mode", "Logging"),
    I("log_verbosity", "--log-verbosity", "Log verbosity", "Logging", 3, aliases=("-lv", "--verbosity"), minimum=0, maximum=5),
    G("log_prefix", "--log-prefix", "--no-log-prefix", "Log prefixes", "Logging"),
    G("log_timestamps", "--log-timestamps", "--no-log-timestamps", "Log timestamps", "Logging"),
    P("log_prompts_dir", "--log-prompts-dir", "Prompt log directory", "Logging"),

    # Upstream convenience configuration flags
    B("embd_gemma_default", "--embd-gemma-default", "EmbeddingGemma defaults", "Advanced"),
    B("fim_qwen_1_5b_default", "--fim-qwen-1.5b-default", "Qwen 1.5B FIM defaults", "Advanced"),
    B("fim_qwen_3b_default", "--fim-qwen-3b-default", "Qwen 3B FIM defaults", "Advanced"),
    B("fim_qwen_7b_default", "--fim-qwen-7b-default", "Qwen 7B FIM defaults", "Advanced"),
    B("fim_qwen_7b_spec", "--fim-qwen-7b-spec", "Qwen 7B speculative defaults", "Advanced"),
    B("fim_qwen_14b_spec", "--fim-qwen-14b-spec", "Qwen 14B speculative defaults", "Advanced"),
    B("fim_qwen_30b_default", "--fim-qwen-30b-default", "Qwen 30B FIM defaults", "Advanced"),
    B("gpt_oss_20b_default", "--gpt-oss-20b-default", "gpt-oss 20B defaults", "Advanced"),
    B("gpt_oss_120b_default", "--gpt-oss-120b-default", "gpt-oss 120B defaults", "Advanced"),
    B("vision_gemma_4b_default", "--vision-gemma-4b-default", "Gemma 4B vision defaults", "Advanced"),
    B("vision_gemma_12b_default", "--vision-gemma-12b-default", "Gemma 12B vision defaults", "Advanced"),
)

# Preserve the historical argv ordering for existing presets. New declarations
# follow in catalogue order and remain disabled unless explicitly selected.
_LEGACY_ORDER = (
    "alias", "host", "port", "ctx_size", "parallel", "flash_attn",
    "cache_type_k", "cache_type_v", "jinja", "n_predict", "threads",
    "threads_batch", "batch_size", "ubatch_size", "priority", "poll",
    "gpu_layers", "device", "main_gpu", "split_mode", "tensor_split",
    "load_mode", "fit", "temperature", "top_p", "top_k", "min_p",
    "repeat_penalty", "no_mmap", "mlock", "numa", "cpu_moe", "n_cpu_moe",
    "override_tensor", "api_key", "timeout", "threads_http", "metrics",
    "cache_prompt", "rope_scaling", "rope_scale", "swa_full",
    "no_kv_offload", "check_tensors", "log_verbosity",
)
_legacy_rank = {key: index for index, key in enumerate(_LEGACY_ORDER)}
_catalogue_rank = {spec.key: index for index, spec in enumerate(_ALL_PARAMETER_SPECS)}
PARAMETER_SPECS = tuple(
    sorted(
        _ALL_PARAMETER_SPECS,
        key=lambda spec: (
            0 if spec.key in _legacy_rank else 1,
            _legacy_rank.get(spec.key, _catalogue_rank[spec.key]),
        ),
    )
)


SPEC_BY_KEY = {spec.key: spec for spec in PARAMETER_SPECS}
CATEGORIES = (
    "General", "Sampling", "Performance", "Memory / GPU", "Context / KV cache",
    "MoE", "Models / Adapters", "Speculative", "Multimodal", "Server / API",
    "Templates / Reasoning", "Advanced",
)
