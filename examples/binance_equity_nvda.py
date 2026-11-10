"""NVDA stock-spot discovery, quote and finite live observation, explicitly opted in."""
import sys

from factorlab.integrations.binance_equity.__main__ import main


if __name__ == '__main__':
    main(['smoke', '--symbol', 'NVDA', *sys.argv[1:]])
