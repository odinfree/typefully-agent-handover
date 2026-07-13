"""Shared lane fixture: gives typefully_lib a synthetic lane config."""

import json
import os
import pathlib
import tempfile

if "TYPEFULLY_LANE_FILE" not in os.environ:
    _lane = pathlib.Path(tempfile.mkdtemp()) / "lane.json"
    _lane.write_text(
        json.dumps(
            {
                "write_set": 111222,
                "expected_targets": {
                    "x": "example_x",
                    "linkedin": "example-linkedin",
                    "threads": "example_threads",
                    "bluesky": "example.bsky.social",
                },
            }
        )
    )
    os.environ["TYPEFULLY_LANE_FILE"] = str(_lane)
