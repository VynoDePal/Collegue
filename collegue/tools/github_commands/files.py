"""
File Commands for GitHub Operations.

Handles file content operations (read/update).
"""

import base64
from typing import Any, Dict, Optional

from ..base import ToolExecutionError
from ..clients import GitHubClient


class FileCommands(GitHubClient):
    def get_file_content(self, owner: str, repo: str, path: str, branch: Optional[str] = None) -> Dict[str, Any]:
        url = f"/repos/{owner}/{repo}/contents/{path}"
        if branch:
            url += f"?ref={branch}"

        resp = self._api_get(url)

        if isinstance(resp, dict) and resp.get("type") == "file":
            content = base64.b64decode(resp["content"]).decode("utf-8")
            return {
                "content": content,
                "sha": resp["sha"],
                "size": resp.get("size", 0),
                "html_url": resp.get("html_url"),
            }
        else:
            raise ToolExecutionError(f"Path '{path}' is not a file or not found")

    def update_file(
        self, owner: str, repo: str, path: str, message: str, content: str, branch: Optional[str] = None
    ) -> Dict[str, Any]:
        sha = None
        try:
            current = self.get_file_content(owner, repo, path, branch)
            sha = current.get("sha")
        except ToolExecutionError:
            pass

        encoded_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")

        data = {"message": message, "content": encoded_content}
        if sha:
            data["sha"] = sha
        if branch:
            data["branch"] = branch

        resp = self._api_put(f"/repos/{owner}/{repo}/contents/{path}", data)
        return {"content": resp.get("content", {}), "commit": resp.get("commit", {})}

    def delete_file(
        self, owner: str, repo: str, path: str, message: str, branch: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Delete a file from the repo. No-op (returns None) if the file is absent.

        The Contents API requires the current blob ``sha`` to delete; we look it up
        first and skip cleanly if the file does not exist on ``branch``.
        """
        try:
            current = self.get_file_content(owner, repo, path, branch)
        except ToolExecutionError:
            return None
        sha = current.get("sha")
        if not sha:
            return None

        data: Dict[str, Any] = {"message": message, "sha": sha}
        if branch:
            data["branch"] = branch

        resp = self._request_json("DELETE", f"/repos/{owner}/{repo}/contents/{path}", json_data=data)
        return {"commit": (resp or {}).get("commit", {})}
