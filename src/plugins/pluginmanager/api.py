"""API routes for installing, updating, and removing third-party DashPi plugins."""

import os
import subprocess
import threading
import time
import uuid
from urllib.parse import urlparse

from flask import Blueprint, current_app, jsonify, request

plugin_manage_bp = Blueprint("pluginmanager_api", __name__)

_JOBS = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL_SECONDS = 300


def _create_job():
    job_id = str(uuid.uuid4())
    job = {
        "lines": [],
        "done": False,
        "success": None,
        "error": None,
        "created_at": time.time(),
        "lock": threading.Lock(),
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job
    return job_id, job


def _get_job(job_id):
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


def _purge_old_jobs():
    cutoff = time.time() - _JOB_TTL_SECONDS
    with _JOBS_LOCK:
        for job_id in [jid for jid, job in _JOBS.items() if job["created_at"] < cutoff]:
            del _JOBS[job_id]


def _run_subprocess_job(job_id, cmd, env, cwd, success_marker):
    job = _get_job(job_id)
    if not job:
        return

    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            if line:
                with job["lock"]:
                    job["lines"].append(line)
        proc.wait()

        output = "\n".join(job["lines"])
        success = proc.returncode == 0 or success_marker in output
        with job["lock"]:
            job["done"] = True
            job["success"] = success
            job["error"] = None if success else "Operation failed. See output above."
    except Exception as exc:
        with job["lock"]:
            job["lines"].append(f"[ERROR] Unexpected error: {exc}")
            job["done"] = True
            job["success"] = False
            job["error"] = str(exc)


def _project_dir():
    from config import Config

    return os.path.dirname(Config.BASE_DIR)


def _cli_script():
    return os.path.join(_project_dir(), "install", "cli", "dashpi-plugin")


def _third_party_plugins():
    device_config = current_app.config["DEVICE_CONFIG"]
    return [plugin for plugin in device_config.get_plugins() if plugin.get("repository")]


def _validate_install_url(url):
    if not url or not isinstance(url, str):
        return False, "URL is required"

    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False, "Invalid URL"

    if parsed.scheme != "https":
        return False, "Only HTTPS URLs are allowed"
    host = parsed.netloc.lower().split(":")[0]
    if host not in ("github.com", "www.github.com"):
        return False, "Only GitHub.com repository URLs are accepted"
    if len([part for part in parsed.path.split("/") if part]) < 2:
        return False, "GitHub repository URL must include owner and repository"
    return True, None


def _operation_env():
    project_dir = _project_dir()
    env = {**os.environ, "PROJECT_DIR": project_dir}
    env.setdefault("APPNAME", "dashpi")
    default_venv = os.path.join("/usr/local", env["APPNAME"], f"venv_{env['APPNAME']}")
    env.setdefault("VENV_PATH", default_venv)
    return env


def _start_cli_job(args, success_marker):
    cli = _cli_script()
    if not os.path.isfile(cli):
        return None, (jsonify({"success": False, "error": "DashPi plugin CLI not found"}), 500)

    _purge_old_jobs()
    job_id, _ = _create_job()
    thread = threading.Thread(
        target=_run_subprocess_job,
        args=(job_id, ["bash", cli, *args], _operation_env(), _project_dir(), success_marker),
        daemon=True,
    )
    thread.start()
    return job_id, None


@plugin_manage_bp.route("/pluginmanager-api/install", methods=["POST"])
def install_plugin():
    data = request.get_json() or {}
    url = (data.get("url") or "").strip()
    ok, err = _validate_install_url(url)
    if not ok:
        return jsonify({"success": False, "error": err}), 400

    job_id, error_response = _start_cli_job(["install-from-url", url], "[INFO] Done")
    if error_response:
        return error_response
    return jsonify({"success": True, "job_id": job_id})


@plugin_manage_bp.route("/pluginmanager-api/uninstall", methods=["POST"])
def uninstall_plugin():
    data = request.get_json() or {}
    plugin_id = (data.get("plugin_id") or "").strip()
    if not plugin_id:
        return jsonify({"success": False, "error": "plugin_id is required"}), 400

    allowed_ids = {plugin["id"] for plugin in _third_party_plugins()}
    if plugin_id not in allowed_ids:
        return jsonify({"success": False, "error": "Plugin not found or cannot be uninstalled"}), 400

    job_id, error_response = _start_cli_job(["uninstall", plugin_id], "Plugin successfully uninstalled")
    if error_response:
        return error_response
    return jsonify({"success": True, "job_id": job_id})


@plugin_manage_bp.route("/pluginmanager-api/check-updates", methods=["POST"])
def check_updates():
    data = request.get_json() or {}
    plugin_id = (data.get("plugin_id") or "").strip()
    if not plugin_id:
        return jsonify({"success": False, "error": "plugin_id is required"}), 400

    plugin_info = next((p for p in _third_party_plugins() if p["id"] == plugin_id), None)
    if not plugin_info:
        return jsonify({"success": False, "error": "Plugin not found"}), 400

    from config import Config

    plugin_dir = os.path.join(Config.BASE_DIR, "plugins", plugin_id)
    if not os.path.isdir(os.path.join(plugin_dir, ".git")):
        return jsonify({"success": False, "error": "Plugin is not a git repository"}), 400

    try:
        local = subprocess.run(
            ["git", "-C", plugin_dir, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        remote_url = subprocess.run(
            ["git", "-C", plugin_dir, "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if local.returncode != 0 or remote_url.returncode != 0:
            return jsonify({"success": False, "error": "Could not determine current version"}), 500

        remote = subprocess.run(
            ["git", "ls-remote", "--heads", remote_url.stdout.strip()],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if remote.returncode != 0:
            return jsonify({"success": False, "error": "Failed to check remote repository"}), 500

        remote_commit = None
        refs = [line.split() for line in remote.stdout.splitlines() if line.strip()]
        for branch in ("main", "master", "develop"):
            match = next((parts for parts in refs if len(parts) > 1 and parts[1] == f"refs/heads/{branch}"), None)
            if match:
                remote_commit = match[0]
                break
        if not remote_commit and refs:
            remote_commit = refs[0][0]

        return jsonify({
            "success": True,
            "has_updates": bool(remote_commit and remote_commit != local.stdout.strip()),
            "commits_behind": 1 if remote_commit and remote_commit != local.stdout.strip() else 0,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "Check updates timed out"}), 500


@plugin_manage_bp.route("/pluginmanager-api/update", methods=["POST"])
def update_plugin():
    data = request.get_json() or {}
    plugin_id = (data.get("plugin_id") or "").strip()
    if not plugin_id:
        return jsonify({"success": False, "error": "plugin_id is required"}), 400

    plugin_info = next((p for p in _third_party_plugins() if p["id"] == plugin_id), None)
    if not plugin_info:
        return jsonify({"success": False, "error": "Plugin not found or cannot be updated"}), 400

    repo_url = (plugin_info.get("repository") or "").strip()
    if not repo_url:
        return jsonify({"success": False, "error": "Plugin repository URL not found"}), 400

    job_id, error_response = _start_cli_job(["install", plugin_id, repo_url], "[INFO] Done")
    if error_response:
        return error_response
    return jsonify({"success": True, "job_id": job_id})


@plugin_manage_bp.route("/pluginmanager-api/job/<job_id>/output", methods=["GET"])
def job_output(job_id):
    job = _get_job(job_id)
    if not job:
        return jsonify({"success": False, "error": "Job not found"}), 404

    since = request.args.get("since", 0, type=int)
    with job["lock"]:
        lines = job["lines"][since:]
        return jsonify({
            "success": True,
            "lines": lines,
            "offset": since + len(lines),
            "done": job["done"],
            "job_success": job["success"],
            "error": job["error"],
        })
