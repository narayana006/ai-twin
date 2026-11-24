"""Run SadTalker's inference.py on a chosen device (it only knows cuda/cpu).
Usage: <sadtalker venv python> sadtalker_run.py <device> <inference.py args...>   (cwd = SadTalker dir)"""
import os, sys
device = sys.argv.pop(1)
sys.path.insert(0, os.getcwd())

if device == "mps":
    # SadTalker does tensor.type(other.type()), and torch can't parse 'torch.mps.FloatTensor'.
    import torch
    _orig_type = torch.Tensor.type

    def _type(self, dtype=None, *a, **k):
        if isinstance(dtype, str) and dtype.startswith("torch.mps."):
            return _orig_type(self, "torch." + dtype.rsplit(".", 1)[-1], *a, **k).to("mps")
        return _orig_type(self, dtype, *a, **k)

    torch.Tensor.type = _type
src = open("inference.py").read().replace('args.device = "cpu"', f'args.device = "{device}"')
sys.argv[0] = "inference.py"
exec(compile(src, "inference.py", "exec"), {"__name__": "__main__", "__file__": os.path.abspath("inference.py")})
