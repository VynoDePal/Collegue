import requests

class GitHubIntegration:
    def __init__(self, token):
        self.headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json'
        }

    def create_or_update_branch(self, repo, branch_name, sha):
        url = f'https://api.github.com/repos/{repo}/git/refs/heads/{branch_name}'
        
        # Vérifier si la branche existe déjà
        response = requests.get(url, headers=self.headers)
        
        if response.status_code == 200:
            # Branche existe, la mettre à jour
            data = {'sha': sha}
            update_response = requests.patch(url, json=data, headers=self.headers)
            if update_response.status_code != 200:
                raise Exception(f'Failed to update branch: {update_response.json()}')
        elif response.status_code == 404:
            # Branche n'existe pas, la créer
            create_url = f'https://api.github.com/repos/{repo}/git/refs'
            data = {'ref': f'refs/heads/{branch_name}', 'sha': sha}
            create_response = requests.post(create_url, json=data, headers=self.headers)
            if create_response.status_code != 201:
                raise Exception(f'Failed to create branch: {create_response.json()}')
        else:
            raise Exception(f'Unexpected error checking branch: {response.json()}')

# Exemple d'utilisation (à adapter dans le serveur MCP)
# integration = GitHubIntegration(token='your_token')
# integration.create_or_update_branch('owner/repo', 'new-branch', 'commit_sha')