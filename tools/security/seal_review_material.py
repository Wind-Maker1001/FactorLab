"""Create review material, never an approval. Run only after source edits finish."""
import hashlib
import json
from pathlib import Path
import shutil
from fixed_validator import PROTOCOL, ANCHOR, inventory

ROOT = Path(__file__).resolve().parents[2]


def main():
    anchor = {"schema": PROTOCOL, "version": 1, "project": "FactorLab",
              "baseline": "98b133841a5cd5ecffcf715e8626b41db2c5b8b6",
              "approval": None, "files": inventory(ROOT)}
    path = ROOT / ANCHOR
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(anchor, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    dest = ROOT / "local-p0/fixed-review-material"
    dest.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(path, dest / "trust-anchor.json")
    shutil.copyfile(Path(__file__).with_name("fixed_validator.py"), dest / "fixed_validator.py")
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.iterdir()}
    (dest / "identity.json").write_text(json.dumps({"status": "BLOCKED", "approval": None,
        "files": hashes, "warning": "Contributor-created review material; maintainer must independently retain and review these pins."}, indent=2), encoding="utf-8")
    print(json.dumps(hashes))


if __name__ == "__main__":
    main()
