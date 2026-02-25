"""
Tests unitaires pour les utilitaires de sécurité fichier.
"""
import pytest
import os
import tempfile
import fcntl
from unittest.mock import patch, MagicMock
from collegue.core.file_security import safe_read_file, safe_getsize, FileSecurityError


class TestFileSecurity:
    
    def test_safe_read_file_success(self):
        """Test la lecture normale d'un fichier."""
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
            f.write("Hello, World!")
            temp_path = f.name
        
        try:
            content = safe_read_file(temp_path, max_size=1024)
            assert content == "Hello, World!"
        finally:
            os.unlink(temp_path)
    
    def test_safe_read_file_size_limit(self):
        """Test que les fichiers trop grands sont rejetés."""
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
            f.write("A" * 1000)
            temp_path = f.name
        
        try:
            with pytest.raises(FileSecurityError, match="File too large"):
                safe_read_file(temp_path, max_size=100)
        finally:
            os.unlink(temp_path)
    
    def test_safe_read_file_symlink_blocked(self):
        """Test que les symlinks sont bloqués."""
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
            f.write("secret content")
            real_file = f.name
        
        # Créer un symlink vers le fichier
        symlink_path = real_file + "_link"
        os.symlink(real_file, symlink_path)
        
        try:
            with pytest.raises(FileSecurityError, match="Symlink"):
                safe_read_file(symlink_path, max_size=1024)
        finally:
            os.unlink(symlink_path)
            os.unlink(real_file)
    
    def test_safe_read_file_path_traversal(self):
        """Test que la traversée de chemin est détectée."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Créer un fichier dans le répertoire temporaire
            safe_file = os.path.join(tmpdir, "safe.txt")
            with open(safe_file, 'w') as f:
                f.write("safe content")
            
            # Essayer de lire un fichier en dehors du répertoire
            outside_file = "/etc/passwd"
            if os.path.exists(outside_file):
                with pytest.raises(FileSecurityError, match="Path traversal"):
                    safe_read_file(outside_file, max_size=1024, base_dir=tmpdir)
    
    def test_safe_read_file_not_regular_file(self):
        """Test que les non-fichiers (répertoires) sont rejetés."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(FileSecurityError, match="Not a regular file"):
                safe_read_file(tmpdir, max_size=1024)
    
    def test_safe_getsize_success(self):
        """Test l'obtention de la taille d'un fichier."""
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
            f.write("Hello")
            temp_path = f.name
        
        try:
            size = safe_getsize(temp_path)
            assert size == 5
        finally:
            os.unlink(temp_path)
    
    def test_safe_getsize_symlink_blocked(self):
        """Test que safe_getsize bloque les symlinks."""
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
            f.write("content")
            real_file = f.name
        
        symlink_path = real_file + "_link"
        os.symlink(real_file, symlink_path)
        
        try:
            with pytest.raises(FileSecurityError, match="Symlink"):
                safe_getsize(symlink_path)
        finally:
            os.unlink(symlink_path)
            os.unlink(real_file)
