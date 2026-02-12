"""
File Commands for GitHub Operations.

Handles file content operations (read/update).
"""
import base64
from typing import Optional, Dict, Any
from ..clients import GitHubClient
from ..base import ToolExecutionError


class FileCommands(GitHubClient):
	"""Commands for file operations."""

	def get_file_content(self, owner: str, repo: str, path: str,
					   branch: Optional[str] = None) -> Dict[str, Any]:
		"""Get file content from a repository."""
		url = f"/repos/{owner}/{repo}/contents/{path}"
		if branch:
			url += f"?ref={branch}"

		resp = self._api_get(url)

		if isinstance(resp, dict) and resp.get('type') == 'file':
			content = base64.b64decode(resp['content']).decode('utf-8')
			return {
				'content': content,
				'sha': resp['sha'],
				'size': resp.get('size', 0),
				'html_url': resp.get('html_url')
			}
		else:
			raise ToolExecutionError(f"Path '{path}' is not a file or not found")

	def update_file(self, owner: str, repo: str, path: str, message: str,
					content: str, branch: Optional[str] = None) -> Dict[str, Any]:
		"""Update file content in a repository."""
		# Get current file SHA if it exists
		sha = None
		try:
			current = self.get_file_content(owner, repo, path, branch)
			sha = current.get('sha')
		except ToolExecutionError:
			pass  # File doesn't exist, will create

		encoded_content = base64.b64encode(content.encode('utf-8')).decode('utf-8')

		data = {
			"message": message,
			"content": encoded_content
		}
		if sha:
			data["sha"] = sha
		if branch:
			data["branch"] = branch

		resp = self._api_put(f"/repos/{owner}/{repo}/contents/{path}", data)
		return {
			"content": resp.get('content', {}),
			"commit": resp.get('commit', {})
		}
