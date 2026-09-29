"""Deployment adapter ``gitops``: the desired state goes into git, a GitOps controller
(Argo CD, Flux) applies it, and the serving system reports what it actually serves.

``deploy`` writes one manifest per model into the checkout at GITOPS_REPO_DIR (path
GITOPS_MANIFEST_PATH, which may use {model} and {name}; content from GITOPS_MANIFEST_TEMPLATE, or a JSON document), commits it
and, with GITOPS_PUSH, pushes it to GITOPS_REMOTE. A commit in git is not a deployment:
``status`` reads GITOPS_STATUS_URL, which implements the webhook status contract
(``GET ?model=`` -> ``{"version", "ready", ...}``) and is served by the serving system itself,
so the read-back comes from what runs, not from git. Undeploying removes the manifest.

The checkout must belong to this adapter: it commits whatever is staged under the manifest
path only (``git commit -- <path>``), never other files. ``git`` must be on PATH.
"""

from __future__ import annotations

import json
import subprocess
from functools import partial
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING

import httpx

from oran_adapt.adapters.deployment._common import (
    HttpApi,
    StaticToken,
    check_template,
    dns_name,
    parse_status,
    required,
)
from oran_adapt.core.errors import ConfigurationError, DeploymentError, DeploymentUnavailableError
from oran_adapt.ports import AdapterSpec, Capability, DeploymentState, DeploymentTarget

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings
    from oran_adapt.ports import DeploymentPort, ModelRegistryPort


class GitOpsDeployment:
    def __init__(
        self,
        *,
        repo_dir: str,
        manifest_path: str,
        manifest_template: str | None,
        push: bool,
        remote: str,
        branch: str | None,
        author_name: str,
        author_email: str,
        git_timeout_s: float,
        status_api: HttpApi,
    ) -> None:
        self.repo_dir = str(Path(repo_dir).resolve())
        self.manifest_path = check_template(
            "gitops_manifest_path", manifest_path, allowed=frozenset({"model", "name"})
        )
        self.manifest_template = manifest_template
        self.push = push
        self.remote = remote
        self.branch = branch
        self.author_name = author_name
        self.author_email = author_email
        self.git_timeout_s = git_timeout_s
        self.status_api = status_api

    @classmethod
    def from_settings(cls, settings: Settings) -> GitOpsDeployment:
        template: str | None = None
        if settings.gitops_manifest_template:
            try:
                template = Path(settings.gitops_manifest_template).read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigurationError(
                    "GITOPS_MANIFEST_TEMPLATE cannot be read",
                    key="gitops_manifest_template",
                    cause=str(exc),
                ) from exc
        repo = required(settings.gitops_repo_dir, "gitops_repo_dir")
        if not (Path(repo) / ".git").exists():
            raise ConfigurationError(
                "GITOPS_REPO_DIR is not a git checkout", key="gitops_repo_dir", path=repo
            )
        return cls(
            repo_dir=repo,
            manifest_path=settings.gitops_manifest_path,
            manifest_template=template,
            push=settings.gitops_push,
            remote=settings.gitops_remote,
            branch=settings.gitops_branch,
            author_name=settings.gitops_author_name,
            author_email=settings.gitops_author_email,
            git_timeout_s=settings.gitops_git_timeout_s,
            status_api=HttpApi(
                required(settings.gitops_status_url, "gitops_status_url"),
                service="the GitOps status endpoint",
                http_factory=partial(httpx.Client, timeout=settings.deployment_http_timeout_s),
                token=StaticToken(settings.gitops_status_token),
            ),
        )

    # ---- git ---------------------------------------------------------------------------

    def _git(self, *args: str) -> str:
        command = [
            "git", "-c", f"user.name={self.author_name}", "-c", f"user.email={self.author_email}",
            *args,
        ]
        try:
            done = subprocess.run(
                command, cwd=self.repo_dir, capture_output=True, text=True,
                timeout=self.git_timeout_s, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeploymentUnavailableError(
                f"git {args[0]} could not run", repo=self.repo_dir, cause=str(exc)
            ) from exc
        if done.returncode != 0:
            raise DeploymentError(
                f"git {args[0]} failed", repo=self.repo_dir, cause=(done.stderr or done.stdout)[-500:]
            )
        return done.stdout

    def _relative(self, model: str) -> str:
        path = self.manifest_path.format(model=model, name=dns_name("", model))
        resolved = (Path(self.repo_dir) / path).resolve()
        if not resolved.is_relative_to(self.repo_dir):
            raise ConfigurationError(
                "GITOPS_MANIFEST_PATH points outside GITOPS_REPO_DIR", key="gitops_manifest_path"
            )
        return resolved.relative_to(self.repo_dir).as_posix()

    def _manifest(self, target: DeploymentTarget) -> str:
        fields = {
            "model": target.model,
            "name": dns_name("", target.model),
            "version": target.version,
            "source": target.source or "",
        }
        if self.manifest_template is None:
            return json.dumps(fields, indent=2, sort_keys=True) + "\n"
        return Template(self.manifest_template).substitute(fields)

    def _commit(self, relative: str, message: str) -> None:
        if not self._git("status", "--porcelain", "--", relative).strip():
            return  # git already holds this state
        self._git("commit", "-m", message, "--", relative)
        if self.push:
            branch = self.branch or self._git("rev-parse", "--abbrev-ref", "HEAD").strip()
            self._git("push", self.remote, f"HEAD:{branch}")

    # ---- port --------------------------------------------------------------------------

    def ping(self) -> None:
        self._git("rev-parse", "--git-dir")
        self.status_api.call("GET", "", params={"model": "_ping"})

    def status(self, model: str) -> DeploymentState:
        response = self.status_api.call("GET", "", params={"model": model})
        if response.status_code == 404:
            return DeploymentState(model=model, version=None, ready=True)
        if response.status_code >= 400:
            raise DeploymentError(
                f"the GitOps status endpoint answered HTTP {response.status_code}",
                model=model,
                cause=response.text[:500],
            )
        return parse_status(model, response.json(), self.status_api.service)

    def deploy(self, target: DeploymentTarget) -> None:
        relative = self._relative(target.model)
        path = Path(self.repo_dir) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._manifest(target), encoding="utf-8")
        self._git("add", "--", relative)
        self._commit(relative, f"deploy {target.model} version {target.version}")

    def restore(self, model: str, previous: DeploymentTarget | None) -> None:
        if previous is not None:
            self.deploy(previous)
            return
        relative = self._relative(model)
        if self._git("ls-files", "--", relative).strip():
            self._git("rm", "-q", "--", relative)
            self._commit(relative, f"undeploy {model}")


def _build(settings: Settings, registry: ModelRegistryPort) -> DeploymentPort:
    return GitOpsDeployment.from_settings(settings)


SPEC = AdapterSpec(
    capability=Capability(
        port="deployment",
        adapter="gitops",
        description="commit a manifest per model to git (Argo CD / Flux); read back via a status URL",
        features=frozenset({"undeploy", "audit_trail"}),
        config_keys=(
            "gitops_repo_dir", "gitops_manifest_path", "gitops_manifest_template", "gitops_push",
            "gitops_remote", "gitops_branch", "gitops_status_url", "gitops_status_token",
            "gitops_author_name", "gitops_author_email", "gitops_git_timeout_s",
            "deployment_http_timeout_s",
        ),
        required_keys=("gitops_repo_dir", "gitops_status_url"),
    ),
    factory=_build,
)
