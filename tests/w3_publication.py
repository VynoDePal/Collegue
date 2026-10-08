"""Clients GitHub COMPLETS pour les tests qui exécutent une livraison réelle (vague 3, raccord C).

Depuis la vague 3, ``open_pr`` en mode réel vérifie la base et la tête distantes (objets Git), lie une preuve de livraison et la
persiste ; il n'existe aucun drapeau de contournement. Les anciens doubles de publication (PR numéro 101, aucun objet Git) ne
suffisent donc plus. Les tests dont le sujet n'est PAS la publication (pilote, runtime, budget) reçoivent ici un VRAI dépôt Git
distant (``github_fakes.FakeRemote``) cloné de leur dépôt source : mêmes blobs, arbres et commits que ceux calculés par ``git``.
"""

from __future__ import annotations

import os
import tempfile

from github_fakes import FakeRemote


def published_remote(repo: str, base: str = "main") -> FakeRemote:
    """Dépôt distant neuf (dossier distinct par appel) cloné de ``repo``."""
    return FakeRemote(tempfile.mkdtemp(prefix="remote-", dir=os.path.dirname(repo)), repo, base=base)


def published_clients(repo: str, base: str = "main"):
    """``PrClients`` COMPLETS adossés à un vrai dépôt Git distant (voir :func:`published_remote`)."""
    return published_remote(repo, base).clients()
