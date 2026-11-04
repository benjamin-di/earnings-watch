"""The public source gate retains source reports and every required image cell."""

from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class PublicCIContract(unittest.TestCase):
    def workflow(self):
        path = ROOT / ".github/workflows/ci.yml"
        if (ROOT / "publish/overlay/.github/workflows/ci.yml").is_file():
            path = ROOT / "publish/overlay/.github/workflows/ci.yml"
        return yaml.load(path.read_text(), Loader=yaml.BaseLoader)

    def test_source_and_all_image_cells_run_with_bounded_read_only_permissions(self):
        workflow = self.workflow()
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(workflow["on"]["push"]["branches"], ["main"])
        self.assertIn("pull_request", workflow["on"])
        jobs = workflow["jobs"]
        self.assertEqual(set(jobs), {"source", "images"})
        self.assertEqual(jobs["images"]["strategy"]["matrix"], {"arch": ["amd64", "arm64"]})
        self.assertEqual(jobs["images"]["strategy"]["fail-fast"], "false")
        self.assertTrue(any(step.get("run") == "make ci-check" for step in jobs["source"]["steps"]))
        self.assertTrue(
            any(
                step.get("run") == 'python3 scripts/ci-images.py --arch "$ARCH"'
                for step in jobs["images"]["steps"]
            )
        )
        self.assertTrue(
            any(
                step.get("uses") == "docker/setup-qemu-action@v3"
                for step in jobs["images"]["steps"]
            )
        )
        for job in jobs.values():
            self.assertLessEqual(int(job["timeout-minutes"]), 60)
            self.assertEqual(job["runs-on"], "ubuntu-24.04")
            checkout = next(
                step
                for step in job["steps"]
                if step.get("uses", "").startswith("actions/checkout@")
            )
            self.assertEqual(
                checkout["with"], {"ref": "${{ github.sha }}", "persist-credentials": "false"}
            )
            verify = next(step for step in job["steps"] if step["name"] == "Verify checkout")
            self.assertEqual(verify["env"], {"EXPECTED_SHA": "${{ github.sha }}"})
            self.assertIn("git rev-parse HEAD", verify["run"])
            artifact = next(
                step
                for step in job["steps"]
                if step.get("uses", "").startswith("actions/upload-artifact@")
            )
            self.assertEqual(artifact["if"], "always()")
            self.assertEqual(artifact["with"]["if-no-files-found"], "error")
            self.assertEqual(artifact["with"]["overwrite"], "true")
        source_report = jobs["source"]["steps"][-1]["with"]
        self.assertEqual(source_report["path"], "reports/junit/check.xml")
        image_report = jobs["images"]["steps"][-1]["with"]["path"]
        self.assertIn("reports/images/*.json", image_report)
        self.assertIn("reports/images/*.log", image_report)


if __name__ == "__main__":
    unittest.main()
