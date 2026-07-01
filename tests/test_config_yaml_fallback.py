from __future__ import annotations

from pathlib import Path

from disc_steward import config as config_module


def test_load_config_without_pyyaml_supports_project_config_subset(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
        pipeline_root: /tmp/media-pipeline
        database_path: /tmp/disc-steward.sqlite3
        dry_run: true
        minimum_title_duration_seconds: 45
        transfer:
          method: local_mount
          rsync_target:
          ssh_options: []
          eddy_final_roots:
            Movies: /mnt/Eddy/Movies
            Family Videos: /mnt/Eddy/Family Videos
        path_mappings:
          barnabas:
            - controller_path: /mnt/Barnabas/data2/media-pipeline
              barnabas_path: /mnt/data2/media-pipeline
        encoding_profiles:
          - remux_only
          - universal_h264_aac_srt
        jellyfin:
          enabled: false
          api_key: ""
          library_ids: []
        """,
    )
    original_yaml = config_module.yaml

    try:
        config_module.yaml = None
        config = config_module.load_config(config_path)
    finally:
        config_module.yaml = original_yaml

    assert config.pipeline_root == Path("/tmp/media-pipeline")
    assert config.database_path == Path("/tmp/disc-steward.sqlite3")
    assert config.minimum_title_duration_seconds == 45
    assert config.dry_run is True
    assert config.rsync_target is None
    assert config.ssh_options == []
    assert config.eddy_library_roots["Family Videos"] == Path("/mnt/Eddy/Family Videos")
    assert config.path_mappings["barnabas"][0].barnabas_path == Path("/mnt/data2/media-pipeline")
    assert config.encoding_profiles == ["remux_only", "universal_h264_aac_srt"]
    assert config.jellyfin.refresh_enabled is False
    assert config.jellyfin.api_key == ""
    assert config.jellyfin.library_ids == []
