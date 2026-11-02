import json, pathlib, pytest
from tests.policy.test_h05bc_turn_wiring import drig  # noqa
from tests.policy.test_ready_barrier import rig  # noqa
from tests.sim.harness import Simulator
HERE = pathlib.Path(__file__).parent

def flat(d, p=""):
    if isinstance(d, dict):
        out = {}
        for k, v in d.items():
            out.update(flat(v, f"{p}.{k}"))
        return out
    return {p: d}

@pytest.mark.parametrize("name", ["균열", "영도", "설화재", "청람호"])
async def test_d(drig, name):
    script = json.loads((HERE / "scripts" / f"{name}.json").read_text())
    got = await Simulator(drig).run(script)
    exp = json.loads((HERE / "expected" / f"{name}.json").read_text())
    for i, (a, b) in enumerate(zip(got, exp)):
        fa, fb = flat(a["view"]), flat(b["view"])
        d = {k: (fb.get(k), fa.get(k)) for k in set(fa) | set(fb) if fa.get(k) != fb.get(k)}
        if d or a["out"] != b["out"]:
            print(f"\n{name} 턴 {i} {a['say']}", d, "OUT" if a["out"] != b["out"] else "")
            break
