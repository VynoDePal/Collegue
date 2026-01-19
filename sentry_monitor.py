import requests

def sentry_monitor(api_key, project_id, org_slug):
    # Assure-toi que project_id est un entier
    project_id = int(project_id)
    
    url = f'https://sentry.io/api/0/projects/{org_slug}/{project_id}/issues/'
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'
    }
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            issues = response.json()
            print(f'Success: Retrieved {len(issues)} issues.')
        else:
            print(f'Error: {response.status_code} - {response.text}')
    except Exception as e:
        print(f'Exception: {e}')

if __name__ == '__main__':
    # Exemple d'utilisation
    sentry_monitor('your_api_key', '123456', 'your_org_slug')  # project_id as string will be converted to int