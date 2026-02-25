"""
Utilitaires de lecture de fichiers sécurisés.

Ce module fournit des fonctions pour lire des fichiers en toute sécurité,
protégées contre les attaques TOCTOU, les symlinks et les traversées de chemin.
"""
import os
import fcntl
from typing import Optional


class FileSecurityError(Exception):
    """Exception levée lors d'une violation de sécurité fichier."""
    pass


def safe_read_file(filepath: str, max_size: int, base_dir: Optional[str] = None) -> str:
    """
    Lit un fichier de manière sécurisée en préservant contre les attaques TOCTOU.
    
    Protection implémentée:
    - Vérifie que le fichier est dans le répertoire de base autorisé (si spécifié)
    - Bloque les symlinks (O_NOFOLLOW)
    - Vérifie que c'est un fichier régulier (pas un device, fifo, etc.)
    - Vérifie la taille via le file descriptor (pas de race condition)
    - Utilise un verrou partagé pendant la lecture
    
    Args:
        filepath: Chemin du fichier à lire
        max_size: Taille maximale autorisée en octets
        base_dir: Répertoire de base autorisé (pour éviter la traversée de chemin)
        
    Returns:
        Contenu du fichier en tant que chaîne
        
    Raises:
        FileSecurityError: Si une violation de sécurité est détectée
        OSError: Si le fichier ne peut pas être ouvert ou lu
    """
    # Vérification du chemin canonique si un répertoire de base est spécifié
    if base_dir is not None:
        real_path = os.path.realpath(filepath)
        base_path = os.path.realpath(base_dir)
        
        # Vérifier que le fichier est bien dans le répertoire autorisé
        if not real_path.startswith(base_path + os.sep) and real_path != base_path:
            raise FileSecurityError(f'Path traversal detected: {filepath} is outside {base_dir}')
    
    # Ouvrir le fichier avec O_NOFOLLOW pour bloquer les symlinks
    try:
        fd = os.open(filepath, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno == 40:  # ELOOP - trop de niveaux de liens symboliques
            raise FileSecurityError(f'Symlink detected and blocked: {filepath}')
        raise
    
    try:
        # Vérifier les stats via le file descriptor (pas de race condition)
        stat_info = os.fstat(fd)
        
        # Vérifier que c'est un fichier régulier
        if not os.path.isfile(fd):
            raise FileSecurityError(f'Not a regular file: {filepath}')
        
        # Vérifier la taille via le file descriptor (TOCTOU-safe)
        if stat_info.st_size > max_size:
            raise FileSecurityError(f'File too large: {stat_info.st_size} bytes (max: {max_size})')
        
        # Vérifier que le fichier n'est pas un symlink (double vérification)
        # os.O_NOFOLLOW devrait déjà bloquer, mais on vérifie quand même
        try:
            if os.path.islink(filepath):
                raise FileSecurityError(f'Symlink detected: {filepath}')
        except OSError:
            pass  # Si le fichier a été supprimé entre-temps, on continuera avec le fd
        
        # Lire le contenu avec verrouillage partagé
        with os.fdopen(fd, 'r', encoding='utf-8', errors='ignore') as f:
            # Verrouiller le fichier pendant la lecture (optionnel, mais plus sûr)
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                content = f.read(max_size + 1)  # Lire un octet de plus pour vérifier
                
                # Vérifier si le fichier a été modifié pendant la lecture
                current_stat = os.fstat(f.fileno())
                if current_stat.st_size != stat_info.st_size:
                    raise FileSecurityError(f'File size changed during read: {filepath}')
                
                return content[:max_size]  # Tronquer si nécessaire
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except:
        os.close(fd)
        raise


def safe_getsize(filepath: str, base_dir: Optional[str] = None) -> int:
    """
    Obtient la taille d'un fichier de manière sécurisée.
    
    Args:
        filepath: Chemin du fichier
        base_dir: Répertoire de base autorisé
        
    Returns:
        Taille du fichier en octets
        
    Raises:
        FileSecurityError: Si une violation de sécurité est détectée
    """
    # Vérification du chemin canonique
    if base_dir is not None:
        real_path = os.path.realpath(filepath)
        base_path = os.path.realpath(base_dir)
        
        if not real_path.startswith(base_path + os.sep) and real_path != base_path:
            raise FileSecurityError(f'Path traversal detected: {filepath}')
    
    # Ouvrir et vérifier via file descriptor
    try:
        fd = os.open(filepath, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno == 40:
            raise FileSecurityError(f'Symlink detected and blocked: {filepath}')
        raise
    
    try:
        stat_info = os.fstat(fd)
        
        if not os.path.isfile(fd):
            raise FileSecurityError(f'Not a regular file: {filepath}')
        
        return stat_info.st_size
    finally:
        os.close(fd)
