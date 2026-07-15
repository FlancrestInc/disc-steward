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
            Café Clips: /mnt/Eddy/Café Clips
        path_mappings:
          barnabas:
            - controller_path: /mnt/Barnabas/data2/media-pipeline
              barnabas_path: /mnt/data2/media-pipeline
        preview:
          enabled: false
          output_path: /tmp/preview-cache
          worker_enabled: true
          worker_name: barnabas-preview
          max_concurrent_jobs: 2
          ffmpeg_path: /opt/ffmpeg/bin/ffmpeg
          encoder: libx264
          fallback_encoder: h264_nvenc
          height: 360
          quality: 24
          auto_generate: false
          clip_duration_seconds: 45
          delete_after_transfer: false
        encoding_profiles:
          - remux_only
          - universal_h264_aac_srt
        jellyfin:
          enabled: false
          api_key: ""
          library_ids: []
        """,
        encoding="utf-8",
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
    assert config.preview.enabled is False
    assert config.preview.output_path == "/tmp/preview-cache"
    assert config.preview.worker_enabled is True
    assert config.preview.worker_name == "barnabas-preview"
    assert config.preview.max_concurrent_jobs == 2
    assert config.preview.ffmpeg_path == "/opt/ffmpeg/bin/ffmpeg"
    assert config.preview.encoder == "libx264"
    assert config.preview.fallback_encoder == "h264_nvenc"
    assert config.preview.height == 360
    assert config.preview.quality == 24
    assert config.preview.auto_generate is False
    assert config.preview.clip_duration_seconds == 45
    assert config.preview.delete_after_transfer is False
    assert config.jellyfin.refresh_enabled is False
    assert config.jellyfin.api_key == ""
    assert config.jellyfin.library_ids == []
