import json, pathlib, subprocess, tempfile, sys
from thinkcap.config import TaskClass, THINK_START, THINK_END
from thinkcap.policy import BudgetPolicy, should_inject
from thinkcap.trace_parser import ThinkStreamParser
with tempfile.TemporaryDirectory(prefix="thinkcap-demo-") as folder:
    policy_path = pathlib.Path(folder) / "policy.json"
    budgets_path = pathlib.Path(folder) / "budgets.json"
    subprocess.run([sys.executable,"-m","thinkcap.cli","probe","--mock","--policy",str(policy_path),"--budgets",str(budgets_path)],text=True,capture_output=True,check=True)
    policies=json.loads(policy_path.read_text())
    print("mock policy classes:", ", ".join(p["task_class"] for p in policies["budgets"]))
parser = ThinkStreamParser(); budget = BudgetPolicy(TaskClass.RENAME, 10)
parser.feed(THINK_START[:7]); parser.feed(THINK_START[7:] + "x" * 80)
print("inside think:", parser.in_think)
print("estimated threshold reached:", should_inject(parser.state, budget))
parser.feed(THINK_END + "answer"); parser.flush()
print("after closing block:", should_inject(parser.state, budget))
print("completed think blocks:", len(parser.think_blocks))
