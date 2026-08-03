import json
import subprocess

from disc_steward.automatic_review import AutomaticLabel
from disc_steward.disc_matching import ContentCandidate
from disc_steward.hermes_bonus_review import request_hermes_bonus_review
from disc_steward.media_evidence import MediaEvidence, SubtitleExcerpt
from disc_steward.models import ScannedFile
from disc_steward.release_matching import ReleaseFit, ReleaseRanking


def _scanned() -> ScannedFile:
    return ScannedFile(
        path="/media/bonus/title_t01.mkv",
        filename="title_t01.mkv",
        parent_disc_folder="/media/bonus",
        size_bytes=1,
        modified_time=1.0,
        duration_seconds=120.0,
        container_format="matroska",
    )


def test_hermes_bonus_review_parses_strict_json_and_validates_roles():
    commands = []

    def runner(argv, **kwargs):
        commands.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {
                    "suggestions": [
                        {
                            "source_file_id": 7,
                            "role": "music_video",
                            "display_name": "Everything Is Awesome",
                            "extra_type": "music_video",
                            "confidence": 0.96,
                            "evidence": "visible title card",
                        },
                        {
                            "source_file_id": 7,
                            "role": "not_allowed",
                            "display_name": "bad",
                            "confidence": 1.0,
                        },
                    ]
                }
            ),
            stderr="",
        )

    result = request_hermes_bonus_review(
        job_id=8,
        disc_title="BONUS_DISC",
        files=[(7, _scanned(), AutomaticLabel("extra", "Bonus extra", "extra", "heuristic", 0.4))],
        runner=runner,
    )

    assert result[_scanned().path].display_name == "Everything Is Awesome"
    assert result[_scanned().path].role == "music_video"
    assert commands[0][0][:4] == ["hermes", "chat", "-q", commands[0][0][3]]
    assert "Inspect the actual media files" in commands[0][0][3]
    assert commands[0][1]["timeout"] == 300


def test_hermes_bonus_review_includes_candidate_and_media_evidence_packets():
    prompts = []

    def runner(argv, **kwargs):
        prompts.append(argv[3])
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"suggestions": []}), stderr="")

    evidence = MediaEvidence(
        source_file_id=7,
        subtitle_excerpts=[SubtitleExcerpt(1.0, 2.0, "VISIBLE EPISODE TITLE", "title-like subtitle cue", "eng")],
        warnings=["bounded sample only"],
    )
    candidate = ContentCandidate(
        candidate_id="release:s01e01",
        title="Visible Episode Title",
        kind="episode",
        season_number=1,
        episode_number=1,
        source_url="https://example.test/release",
    )
    ranking = ReleaseRanking([ReleaseFit("release", "SHOW_DISC", 0.91, "high_confidence", {"file_count": 1.0})])

    request_hermes_bonus_review(
        job_id=8,
        disc_title="SHOW_DISC",
        files=[(7, _scanned(), AutomaticLabel("episode", "fallback", "", "heuristic", 0.1))],
        candidate_inventory=[candidate],
        media_evidence={7: evidence},
        release_ranking=ranking,
        runner=runner,
    )

    prompt = prompts[0]
    assert '"candidate_inventory"' in prompt
    assert "Visible Episode Title" in prompt
    assert "https://example.test/release" in prompt
    assert '"media_evidence"' in prompt
    assert "VISIBLE EPISODE TITLE" in prompt
    assert "bounded sample only" in prompt
    assert '"release_ranking"' in prompt
    assert "high_confidence" in prompt


def test_hermes_bonus_review_handles_prose_wrapped_json():
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout='Here is the result:\n{"suggestions": []}\n',
            stderr="",
        )

    assert request_hermes_bonus_review(
        job_id=1,
        disc_title="BONUS",
        files=[(1, _scanned(), AutomaticLabel("extra", "Bonus", "extra", "heuristic", 0.3))],
        runner=runner,
    ) == {}


def test_hermes_bonus_review_accepts_main_features_and_episodes():
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {
                    "suggestions": [
                        {
                            "source_file_id": 7,
                            "role": "episode",
                            "content_type": "anime",
                            "display_name": "The First Battle",
                            "extra_type": None,
                            "season_number": 1,
                            "episode_number": 3,
                            "confidence": 0.9,
                            "evidence": "episode title card",
                        }
                    ]
                }
            ),
            stderr="",
        )

    result = request_hermes_bonus_review(
        job_id=1,
        disc_title="SHOW_DISC",
        files=[(7, _scanned(), AutomaticLabel("extra", "fallback", "extra", "heuristic", 0.1))],
        runner=runner,
    )

    label = result[_scanned().path]
    assert label.role == "episode"
    assert label.content_type == "anime"
    assert label.extra_type == ""
    assert label.season_number == 1
    assert label.episode_number == 3
