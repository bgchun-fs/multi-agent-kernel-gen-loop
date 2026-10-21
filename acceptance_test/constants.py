"""The three competition kernels and their official baseline cycles (the paper's Table II), used by acceptance_test.py."""
KERNELS = ("qkv", "attn_out", "ffn")
LONG = {"qkv": "sliding_project_qkv", "attn_out": "sliding_attention_output", "ffn": "decoder_feedforward"}
SHORT = {v: k for k, v in LONG.items()}
BASELINE_CYCLES = {"qkv": 250514, "attn_out": 404633, "ffn": 3703473}
