import os

from app.pipeline.subprocess_utils import subprocess_process_group_kwargs


def test_subprocess_kwargs_create_process_group_for_timeout_cleanup():
    kwargs = subprocess_process_group_kwargs()
    if os.name == "nt":
        assert "creationflags" in kwargs
    else:
        assert kwargs == {"start_new_session": True}
