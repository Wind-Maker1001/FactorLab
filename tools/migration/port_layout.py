"""Port physical layout and identity contracts without altering old receipts."""
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT/'src/factorlab'


def edit(relative, old, new):
    path = SOURCE/relative
    text = path.read_text(encoding='utf-8')
    assert old in text, (relative,old)
    path.write_text(text.replace(old,new),encoding='utf-8',newline='\n')


def main():
    resources = SOURCE/'resources'
    resources.mkdir()
    shutil.copyfile(ROOT/'configs/research/csi500_5y.yaml', resources/'csi500_5y.yaml')
    (resources/'__init__.py').write_text('"""Packaged public configuration resources."""\n',encoding='utf-8')
    edit('config/loader.py','(start or Path(__file__)).resolve()','(start or Path.cwd()).resolve()')
    edit('config/loader.py','(directory / "qbot").is_dir() and (directory / "README.md").is_file()',
         '(directory / "src/factorlab").is_dir() and (directory / "pyproject.toml").is_file()')
    edit('config/loader.py','cannot locate repository root (expected qbot/ and README.md)',
         'cannot locate FactorLab source root (expected src/factorlab and pyproject.toml)')
    edit('config/loader.py','''    root = (
        Path(repo_root).expanduser().resolve()
        if repo_root is not None
        else discover_repository_root()
    )''','''    if repo_root is not None:
        root = Path(repo_root).expanduser().resolve()
    else:
        start = Path(path).expanduser()
        try:
            root = discover_repository_root(start if start.is_absolute() else Path.cwd())
        except ConfigurationError:
            root = start.resolve().parent if start.is_absolute() else Path.cwd().resolve()''')
    edit('config/models.py','name: str = "qbot-factorlab"','name: str = "factorlab"')
    edit('forward/integrity.py','root / "qbot" / "research"','root / "src" / "factorlab"')
    edit('forward/integrity.py','root / "dev" / "requirements-research.txt"','root / "pyproject.toml"')
    edit('forward/integrity.py','qbot-factorlab-source-v1','factorlab-independent-source-v1')
    edit('diagnostics/model_readiness/source_identity.py','factorlab-model-readiness-source-v2','factorlab-independent-model-readiness-source-v1')
    edit('diagnostics/model_readiness/source_identity.py','qbot/research','src/factorlab')
    for relative in ('diagnostics/model_readiness/runner.py','diagnostics/model_readiness/__main__.py',
                     'exploratory/market_structure/runner.py'):
        edit(relative,'from pathlib import Path','from pathlib import Path\nfrom factorlab._identity import source_context_root, software_identity')
        edit(relative,'Path(__file__).resolve().parents[4]','source_context_root()')
    edit('exploratory/market_structure/runner.py','''subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()''', '''software_identity()["git_commit"]''')
    edit('exploratory/factor_discovery/receipts.py','repo = research.parents[1]',
         'from factorlab._identity import source_context_root\n    repo = source_context_root()')
    edit('exploratory/factor_discovery/receipts.py','"qbot/research"','"src/factorlab"')
    edit('exploratory/runner.py','root = Path(__file__).resolve().parents[3]',
         'from factorlab._identity import source_context_root\n    root = source_context_root()')
    edit('exploratory/runner.py','paths.append(root / "dev/requirements-research.txt")',
         'paths.append(Path(__import__("factorlab").__file__).parent / "resources/csi500_5y.yaml")')
    edit('data/csmar/fingerprint.py','csmar-implementation-path-closure-v2','factorlab-csmar-implementation-path-closure-v1')
    edit('data/csmar/fingerprint.py','CONFIGS = ("configs/research/csi500_5y.yaml",)',
         'CONFIGS = ("src/factorlab/resources/csi500_5y.yaml",)')
    edit('data/csmar/fingerprint.py','root = Path(factorlab.__file__).resolve().parents[2]',
         'root = Path(factorlab.__file__).resolve().parent')
    edit('data/csmar/fingerprint.py','''        prefix = "qbot/research/data/csmar/"
        return package / path[len(prefix):] if path.startswith(prefix) else root / path''',
         '''        prefix = "src/factorlab/"
        if not path.startswith(prefix):
            raise ValueError("implementation source outside factorlab")
        return root / path[len(prefix):]''')
    edit('data/csmar/fingerprint.py','qbot/research/','src/factorlab/')
    edit('data/csmar/fingerprint.py','parts[:2] != ["qbot", "research"]','parts[:1] != ["factorlab"]')
    edit('data/csmar/fingerprint.py','name = "/".join(parts[:count])','name = "src/" + "/".join(parts[:count])')
    edit('data/csmar/fingerprint.py','parts = path[:-3].split("/")','parts = path[len("src/"):-3].split("/")')
    edit('data/csmar/fingerprint.py','sha256((root / p).read_bytes())','sha256(physical(p).read_bytes())')
    edit('data/admission.py','''    return {
        "schema": "FACTORLAB_FORMAL_MARKET_ADMISSION_V1",''',
         '''    from factorlab._identity import assert_migration_admitted
    assert_migration_admitted()
    return {
        "schema": "FACTORLAB_FORMAL_MARKET_ADMISSION_V1",''')
    edit('reproducibility.py','''            "run_id": self.run_id,''','''            "software_identity": __import__("factorlab._identity", fromlist=["software_identity"]).software_identity(),
            "run_id": self.run_id,''')
    edit('cli.py','prog="python -m factorlab.cli"','prog="factorlab"')
    edit('cli.py','Qbot FactorLab research pipeline','FactorLab research pipeline')
    edit('cli.py','''    subparsers = parser.add_subparsers(dest="command", required=True)''','''    subparsers = parser.add_subparsers(dest="command", required=True)
    demo = subparsers.add_parser("demo", help="Offline synthetic four-model software demonstration")
    demo.add_argument("--output", required=True, type=Path)
    demo.add_argument("--seed", default=42, type=int)''')
    edit('cli.py','''    arguments = parser.parse_args(argv)''','''    arguments = parser.parse_args(argv)
    if arguments.command == "demo":
        from factorlab.demo import run
        try:
            result = run(arguments.output, seed=arguments.seed)
        except (OSError, ValueError, FactorLabError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(result["warning"])
        print(Path(arguments.output) / "manifest.json")
        return 0''')
    edit('cli.py','''    required = [
        "ARCHITECTURE_AUDIT.md",
        "KNOWN_LEGACY_ISSUES.md",
    ]
    docs_dir = (config.repo_root or Path.cwd()) / "docs" / "research"
    missing = [name for name in required if not (docs_dir / name).is_file()]
    if missing:
        raise FactorLabError("Architecture audit artifacts missing: %s" % missing)
    run.add_output("architecture_audit", docs_dir / required[0])
    LOGGER.info("Architecture audit available at %s", docs_dir / required[0])''','''    from factorlab._identity import software_identity
    identity = software_identity()
    run.payload["architecture_audit"] = identity
    run.payload["formal_admission"] = "FORMAL_ADMISSION_BLOCKED"
    LOGGER.info("FactorLab software identity audited: %d modules; formal admission BLOCKED",
                len(identity["source_sha256"]))''')
    # The public demo shares the exact deterministic numerical probe implementation.
    demo = (ROOT/'tools/migration/behavior_probe.py').read_text(encoding='utf-8')
    demo = demo.replace("parser.add_argument('--namespace', default='factorlab')", "")
    demo = demo.replace('run(args.output,args.namespace,args.input_root)','run(args.output,input_root=args.input_root)')
    (SOURCE/'demo.py').write_text(demo,encoding='utf-8')
    with (ROOT/'pyproject.toml').open('a',encoding='utf-8') as handle:
        handle.write('\n[tool.setuptools.package-data]\nfactorlab = ["resources/*.yaml"]\n')
    print('Ported layout, resource identity and CLI; formal migration remains blocked.')


if __name__ == '__main__':
    main()
