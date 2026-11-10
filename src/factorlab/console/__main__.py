"""Start the local read-only console: python -m factorlab.console --config PATH."""

import argparse

from factorlab.console.data import Catalog
from factorlab.console.server import ConsoleServer


def main():
    parser = argparse.ArgumentParser(description="FactorLab local read-only research console")
    parser.add_argument("--config", required=True, help="Explicit local console JSON configuration")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with ConsoleServer(Catalog(args.config), args.port) as server:
        print(f"FactorLab Research Console: http://127.0.0.1:{server.server_port}", flush=True)
        print("NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
