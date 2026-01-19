import requests

class GitHubClient:
    def __init__(self, token, repo):
        self.token = token
        self.repo = repo
        self.base_url = f"https://api.github.com/repos/{repo}"
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"
        }

    def create_branch(self, branch_name, source_branch="main"):
        # 1. Récupérer le SHA du dernier commit de la branche source
        ref_url = f"{self.base_url}/git/refs/heads/{source_branch}"
        response = requests.get(ref_url, headers=self.headers)
        response.raise_for_status()
        sha = response.json()["object"]["sha"]

        # 2. Tenter de créer la nouvelle branche
        create_ref_url = f"{self.base_url}/git/refs"
        payload = {
            "ref": f"refs/heads/{branch_name}",
            "sha": sha
        }
        
        res = requests.post(create_ref_url, json=payload, headers=self.headers)
        
        if res.status_code == 422 and "Reference already exists" in res.text:
            # La branche existe déjà, on ignore l'erreur ou on logue
            print(f"La branche {branch_name} existe déjà.")
            return True
        
        res.raise_for_status()
        return res.json()