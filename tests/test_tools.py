import sys
import os
sys.path.insert(0, '/root/.openclaw/workspace/Collegue')
from collegue.tools.dependency_guard import DependencyGuardTool, DependencyGuardRequest
from collegue.tools.repo_consistency_check import RepoConsistencyCheckTool, ConsistencyCheckRequest, FileInput as ConsistencyFileInput
from collegue.tools.secret_scan import SecretScanTool, SecretScanRequest, FileContent
from collegue.tools.impact_analysis import ImpactAnalysisTool, ImpactAnalysisRequest, FileInput as ImpactFileInput
print("=" * 80)
print("TEST 1: DEPENDENCY GUARD (avec cache OSV)")
print("=" * 80)
dep_tool = DependencyGuardTool()
with open('/root/.openclaw/workspace/Collegue/requirements.txt', 'r') as f:
    req_content = f.read()
dep_request = DependencyGuardRequest(
    content=req_content,
    language="python",
    check_vulnerabilities=True,
    check_existence=True
)
try:
    dep_response = dep_tool.execute(request=dep_request)
    print(f"✅ Résultat: {dep_response.summary}")
    print(f"   Total dépendances: {dep_response.total_dependencies}")
    print(f"   Vulnérabilités: {dep_response.vulnerabilities}")
    print(f"   Par sévérité: Critique({dep_response.critical}), Haute({dep_response.high}), Moyenne({dep_response.medium}), Basse({dep_response.low})")
    if dep_response.issues:
        print(f"   Issues trouvées: {len(dep_response.issues)}")
        for issue in dep_response.issues[:5]:
            print(f"     - {issue.package}: {issue.message}")
except Exception as e:
    print(f"❌ Erreur: {e}")
    import traceback
    traceback.print_exc()
print("\n" + "=" * 80)
print("TEST 2: REPO CONSISTENCY CHECK (avec JSParser)")
print("=" * 80)
consistency_tool = RepoConsistencyCheckTool()
files_to_check = []
collegue_dir = '/root/.openclaw/workspace/Collegue/collegue'
for root, dirs, files in os.walk(collegue_dir):
    dirs[:] = [d for d in dirs if d not in ['__pycache__', '.venv', 'node_modules']]
    for file in files:
        if file.endswith(('.py', '.js')):
            filepath = os.path.join(root, file)
            try:
                with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                rel_path = os.path.relpath(filepath, '/root/.openclaw/workspace/Collegue')
                files_to_check.append(ConsistencyFileInput(path=rel_path, content=content))
                if len(files_to_check) >= 10:
                    break
            except Exception as e:
                print(f"   Ignoré {filepath}: {e}")
    if len(files_to_check) >= 10:
        break
print(f"   Fichiers à analyser: {len(files_to_check)}")
for f in files_to_check:
    print(f"     - {f.path}")
consistency_request = ConsistencyCheckRequest(
    files=files_to_check,
    checks=["unused_imports", "unused_vars", "dead_code"],
    analysis_depth="fast"
)
try:
    consistency_response = consistency_tool.execute(request=consistency_request)
    print(f"\n✅ Résultat: {consistency_response.summary}")
    print(f"   Fichiers analysés: {consistency_response.files_analyzed}")
    print(f"   Issues trouvées: {consistency_response.summary.get('total', 0)}")
    print(f"   Score refactoring: {consistency_response.refactoring_score:.2f}")
    if consistency_response.issues:
        print(f"   Détail des issues:")
        for issue in consistency_response.issues[:10]:
            print(f"     - [{issue.kind}] {issue.path}:{issue.line} - {issue.message} (confiance: {issue.confidence}%)")
except Exception as e:
    print(f"❌ Erreur: {e}")
    import traceback
    traceback.print_exc()
print("\n" + "=" * 80)
print("TEST 3: SECRET SCAN (avec détection entropie)")
print("=" * 80)
secret_tool = SecretScanTool()
secret_files = []
scan_dirs = [
    '/root/.openclaw/workspace/Collegue/collegue',
    '/root/.openclaw/workspace/Collegue/tests',
]
for scan_dir in scan_dirs:
    if os.path.exists(scan_dir):
        for root, dirs, files in os.walk(scan_dir):
            dirs[:] = [d for d in dirs if d not in ['__pycache__', '.venv', 'node_modules']]
            for file in files:
                if file.endswith(('.py', '.js', '.ts', '.json', '.yaml', '.yml', '.env', '.sh')):
                    filepath = os.path.join(root, file)
                    try:
                        if os.path.getsize(filepath) > 1024 * 1024:
                            continue
                        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read()
                        rel_path = os.path.relpath(filepath, '/root/.openclaw/workspace/Collegue')
                        secret_files.append(FileContent(path=rel_path, content=content))
                        if len(secret_files) >= 20:
                            break
                    except Exception as e:
                        pass
            if len(secret_files) >= 20:
                break
    if len(secret_files) >= 20:
        break
print(f"   Fichiers à scanner: {len(secret_files)}")
secret_request = SecretScanRequest(
    files=secret_files,
    severity_threshold="low",
    scan_type="batch"
)
try:
    secret_response = secret_tool.execute(request=secret_request)
    print(f"\n✅ Résultat: {secret_response.scan_summary}")
    print(f"   Fichiers scannés: {secret_response.files_scanned}")
    print(f"   Secrets trouvés: {secret_response.total_findings}")
    print(f"   Par sévérité: Critique({secret_response.critical}), Haute({secret_response.high}), Moyenne({secret_response.medium}), Basse({secret_response.low})")
    if secret_response.findings:
        print(f"   Détail des secrets:")
        for finding in secret_response.findings[:10]:
            print(f"     - [{finding.severity}] {finding.type} dans {finding.file}:{finding.line}")
            print(f"       Match: {finding.match[:80]}...")
            print(f"       Règle: {finding.rule}")
except Exception as e:
    print(f"❌ Erreur: {e}")
    import traceback
    traceback.print_exc()
print("\n" + "=" * 80)
print("TEST 4: IMPACT ANALYSIS (avec graphe de dépendances)")
print("=" * 80)
impact_tool = ImpactAnalysisTool()
files_to_analyze = []
collegue_dir = '/root/.openclaw/workspace/Collegue/collegue'
for root, dirs, files in os.walk(collegue_dir):
    dirs[:] = [d for d in dirs if d not in ['__pycache__', '.venv', 'node_modules']]
    for file in files:
        if file.endswith('.py'):
            filepath = os.path.join(root, file)
            try:
                with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                rel_path = os.path.relpath(filepath, '/root/.openclaw/workspace/Collegue')
                files_to_analyze.append(ImpactFileInput(path=rel_path, content=content))
                if len(files_to_analyze) >= 8:
                    break
            except:
                pass
    if len(files_to_analyze) >= 8:
        break
print(f"   Fichiers à analyser: {len(files_to_analyze)}")
for f in files_to_analyze:
    print(f"     - {f.path}")
impact_request = ImpactAnalysisRequest(
    change_intent="Modifier la classe BaseTool pour ajouter une méthode get_metadata()",
    files=files_to_analyze,
    analysis_depth="fast",
    confidence_mode="balanced"
)
try:
    impact_response = impact_tool.execute(request=impact_request)
    print(f"\n✅ Résultat: {impact_response.analysis_summary}")
    print(f"   Fichiers impactés: {len(impact_response.impacted_files)}")
    print(f"   Risques: {len(impact_response.risk_notes)}")
    print(f"   Tests recommandés: {len(impact_response.tests_to_run)}")
    if impact_response.impacted_files:
        print(f"   Détail fichiers impactés:")
        for f in impact_response.impacted_files[:5]:
            print(f"     - {f.path}: {f.reason[:60]}...")
    if impact_response.risk_notes:
        print(f"   Risques détectés:")
        for r in impact_response.risk_notes[:3]:
            print(f"     - [{r.severity}] {r.category}: {r.note[:60]}...")
except Exception as e:
    print(f"❌ Erreur: {e}")
    import traceback
    traceback.print_exc()
print("\n" + "=" * 80)
print("TESTS TERMINÉS")
print("=" * 80)