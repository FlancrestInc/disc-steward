from disc_steward.cli import build_parser


def test_migrate_command_requires_explicit_apply_flag():
    args = build_parser().parse_args(["migrate", "--config", "config.example.yaml"])
    assert args.command == "migrate"
    assert args.apply is False

    applied = build_parser().parse_args(["migrate", "--apply", "--config", "config.example.yaml"])
    assert applied.apply is True
