import requests

def get_sentry_projects():
    base_url = 'https://sentry.io'  # Remplacez par votre instance Sentry si nécessaire
    org_slug = 'collegue'
    url = f'/api/0/organizations/{org_slug}/projects/'
    headers = {
        'Authorization': 'Bearer YOUR_SENTRY_TOKEN'  # Remplacez par votre token
    }
    try:
        response = requests.get(base_url + url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        if response.status_code == 404:
            print(f'Ressource introuvable: {url}')
        else:
            print(f'Erreur HTTP: {e}')
    except Exception as e:
        print(f'Erreur lors de la récupération des données Sentry: {e}')
        return None

if __name__ == '__main__':
    projects = get_sentry_projects()
    if projects:
        print('Projets récupérés:', projects)