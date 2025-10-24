from pathlib import Path
import sys

# Ensure original project root is on path
PROJECT_ROOT = Path('/root')
if str(PROJECT_ROOT) not in sys.path:
	sys.path.append(str(PROJECT_ROOT))

# Import existing, proven trading logic without changes
from bot_22_A import PairCfg, UserCfg, SymbolBot, BinanceREST, ws_prices, load_json  # type: ignore

__all__ = [
	"PairCfg",
	"UserCfg",
	"SymbolBot",
	"BinanceREST",
	"ws_prices",
	"load_json",
] 