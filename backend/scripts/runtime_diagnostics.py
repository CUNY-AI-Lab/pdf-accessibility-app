from __future__ import annotations

import json

from app.services.runtime_diagnostics import collect_runtime_diagnostics


def main() -> None:
    print(json.dumps(collect_runtime_diagnostics(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
