import logging
import os
import sys
from abc import ABC, abstractmethod
from functools import partial
from multiprocessing import Pool
from typing import Iterable, List, Optional, Dict

import numpy as np
import rdkit
from diskcache import FanoutCache, Cache
from rdkit import Chem, RDConfig
from rdkit.Chem.EnumerateStereoisomers import EnumerateStereoisomers, StereoEnumerationOptions
from rdkit.Chem.MolStandardize import rdMolStandardize
from rdkit.Chem.rdDistGeom import EmbedMolecule

from rlmolecule.molecule.builder.gdb_filters import check_all_filters

sys.path.append(os.path.join(RDConfig.RDContribDir, 'SA_Score'))
# noinspection PyUnresolvedReferences
import sascorer

pt = Chem.GetPeriodicTable()
tautomer_enumerator = rdMolStandardize.TautomerEnumerator()
tautomer_enumerator.SetMaxTautomers(50)
tautomer_enumerator.SetMaxTransforms(50)

bond_orders = [Chem.BondType.SINGLE, Chem.BondType.DOUBLE, Chem.BondType.TRIPLE]
logger = logging.getLogger(__name__)

from rdkit import RDLogger

RDLogger.DisableLog('rdApp.warning')


class MoleculeBuilder:
    def __init__(self,
                 max_atoms: int = 10,
                 min_atoms: int = 4,
                 atom_additions: Optional[List] = None,
                 stereoisomers: bool = False,
                 canonicalize_tautomers: bool = False,
                 sa_score_threshold: Optional[float] = None,
                 try_embedding: bool = False,
                 cache_dir: Optional[str] = None,
                 num_shards: int = 1,
                 parallel: bool = False) -> None:
        """A class to build molecules according to a number of different options

        :param max_atoms: Maximum number of heavy atoms
        :param min_atoms: minimum number of heavy atoms
        :param atom_additions: potential atom types to consider. Defaults to ('C', 'H', 'O')
        :param stereoisomers: whether to consider stereoisomers different molecules
        :param sa_score_threshold: If set, don't construct molecules greater than a given sa_score.
        :param try_embedding: Try to get a 3D embedding of the molecule, and if this fails, remote it.
        """

        # Not the most elegant solution, these are carried and referenced by MoleculeState, but are not used internally
        self.parallel = parallel
        self.max_atoms = max_atoms
        self.min_atoms = min_atoms

        if cache_dir is not None:
            if num_shards == 1:
                cache = Cache(directory=cache_dir)
            else:
                cache = FanoutCache(directory=cache_dir, shards=num_shards)

            self.cached_call = cache.memoize()(self.call)

        else:
            self.mem = None
            self.cached_call = None

        self.transformation_stack = []

        if canonicalize_tautomers:
            self.transformation_stack += [TautomerEnumerator()]

        self.transformation_stack += [
            AddNewAtomsAndBonds(atom_additions),
        ]

        parallel_stack = [GdbFilter(), ]

        if sa_score_threshold is not None:
            parallel_stack += [SAScoreFilter(sa_score_threshold, min_atoms)]

        if canonicalize_tautomers:
            parallel_stack += [TautomerCanonicalizer()]

        if stereoisomers:
            parallel_stack += [StereoEnumerator()]

        if not parallel:
            # If we're not running in parallel, reduce to unique molecules before embedding
            parallel_stack += [UniqueMoleculeFilter()]

        if try_embedding:
            parallel_stack += [EmbeddingFilter()]

        if parallel:
            self.transformation_stack += [
                ParallelTransformer(parallel_stack),
                UniqueMoleculeFilter()]

        else:
            self.transformation_stack += parallel_stack

    def call(self, parent_molecule: rdkit.Chem.Mol) -> List[rdkit.Chem.Mol]:
        inputs = [parent_molecule]
        for transformer in self.transformation_stack:
            inputs = transformer(inputs)
        return list(inputs)

    def __call__(self, parent_molecule: rdkit.Chem.Mol) -> Iterable[rdkit.Chem.Mol]:
        if self.cached_call is None:
            return self.call(parent_molecule)

        else:
            return self.cached_call(parent_molecule)

    def __getstate__(self):
        attributes = self.__dict__
        attributes['cached_call'] = None
        return attributes


class BaseTransformer(ABC):
    @abstractmethod
    def __call__(self, inputs: Iterable[rdkit.Chem.Mol]) -> Iterable[rdkit.Chem.Mol]:
        pass


class MoleculeTransformer(BaseTransformer, ABC):
    @abstractmethod
    def call(self, molecule: rdkit.Chem.Mol) -> Iterable[rdkit.Chem.Mol]:
        pass

    def __call__(self, inputs: Iterable[rdkit.Chem.Mol]) -> Iterable[rdkit.Chem.Mol]:
        for molecule in inputs:
            yield from self.call(molecule)


def process_call(molecule: rdkit.Chem.Mol, transformation_stack: List[MoleculeTransformer]) -> List[rdkit.Chem.Mol]:
    inputs = (molecule,)
    for transformer in transformation_stack:
        inputs = transformer(inputs)
    return list(inputs)


class ParallelTransformer(BaseTransformer):
    def __init__(self,
                 transformation_stack: List[MoleculeTransformer],
                 chunk_size: int = 10):
        self.chunk_size = chunk_size
        self.transformation_stack = transformation_stack
        self.pool = Pool()

    def __call__(self, inputs: Iterable[rdkit.Chem.Mol]) -> Iterable[rdkit.Chem.Mol]:
        call_fn = partial(process_call, transformation_stack=self.transformation_stack)
        for result in self.pool.imap_unordered(call_fn, inputs, self.chunk_size):
            yield from result


class MoleculeFilter(MoleculeTransformer, ABC):
    @abstractmethod
    def filter(self, molecule: rdkit.Chem.Mol) -> bool:
        pass

    def call(self, molecule: rdkit.Chem.Mol) -> Iterable[rdkit.Chem.Mol]:
        if self.filter(molecule):
            yield molecule


class UniqueMoleculeFilter(BaseTransformer, ABC):
    def __call__(self, inputs: Iterable[rdkit.Chem.Mol]) -> Iterable[rdkit.Chem.Mol]:
        self.seen_smiles = set()
        for molecule in inputs:
            smiles = rdkit.Chem.MolToSmiles(molecule)
            if smiles not in self.seen_smiles:
                yield molecule
                self.seen_smiles.add(smiles)


class AddNewAtomsAndBonds(MoleculeTransformer):
    def __init__(self, atom_additions: Optional[List] = None, **kwargs):
        super(AddNewAtomsAndBonds, self).__init__(**kwargs)
        if atom_additions is not None:
            self.atom_additions = atom_additions
        else:
            self.atom_additions = ('C', 'N', 'O')

    @staticmethod
    def sanitize(molecule: rdkit.Chem.Mol) -> Optional[rdkit.Chem.Mol]:
        """Sanitize the output molecules, as the RWmols don't have the correct initialization of rings and valence.
        Would be good to debug faster versions of this.

        :param molecule: rdkit RWmol or variant
        :return: sanitized molecule, or None if failed
        """
        # molecule = rdkit.Chem.Mol(molecule)
        # rdkit.Chem.SanitizeMol(molecule)
        # return molecule
        return rdkit.Chem.MolFromSmiles(rdkit.Chem.MolToSmiles(molecule))

    def call(self, molecule: rdkit.Chem.Mol) -> Iterable[rdkit.Chem.Mol]:
        for i, atom in enumerate(molecule.GetAtoms()):
            for partner in self._get_valid_partners(molecule, atom):
                for bond_order in self._get_valid_bonds(molecule, i, partner):
                    rw_mol = self._add_bond(molecule, i, partner, bond_order)
                    sanitized_mol = self.sanitize(rw_mol)
                    if sanitized_mol is not None:
                        yield sanitized_mol

    @staticmethod
    def _get_free_valence(atom) -> int:
        """ For a given atom, calculate the free valence remaining """
        return pt.GetDefaultValence(atom.GetSymbol()) - atom.GetExplicitValence()

    def _get_valid_partners(self, starting_mol: rdkit.Chem.Mol, atom: rdkit.Chem.Atom) -> List[int]:
        """ For a given atom, return other atoms it can be connected to """
        return list(
            set(range(starting_mol.GetNumAtoms())) - set((neighbor.GetIdx() for neighbor in atom.GetNeighbors())) -
            set(range(atom.GetIdx())) -  # Prevent duplicates by only bonding forward
            {atom.GetIdx()} | set(np.arange(len(self.atom_additions)) + starting_mol.GetNumAtoms()))

    def _get_valid_bonds(self, starting_mol: rdkit.Chem.Mol, atom1_idx: int, atom2_idx: int) -> range:
        """ Compare free valences of two atoms to calculate valid bonds """
        free_valence_1 = self._get_free_valence(starting_mol.GetAtomWithIdx(atom1_idx))
        if atom2_idx < starting_mol.GetNumAtoms():
            free_valence_2 = self._get_free_valence(starting_mol.GetAtomWithIdx(int(atom2_idx)))
        else:
            free_valence_2 = pt.GetDefaultValence(self.atom_additions[atom2_idx - starting_mol.GetNumAtoms()])

        return range(min(min(free_valence_1, free_valence_2), 3))

    def _add_bond(self, starting_mol: rdkit.Chem.Mol, atom1_idx: int, atom2_idx: int, bond_type: int) -> Chem.RWMol:
        """ Given two atoms and a bond type, execute the addition using rdkit """
        num_atom = starting_mol.GetNumAtoms()
        rw_mol = Chem.RWMol(starting_mol)

        if atom2_idx < num_atom:
            rw_mol.AddBond(atom1_idx, atom2_idx, bond_orders[bond_type])

        else:
            rw_mol.AddAtom(Chem.Atom(self.atom_additions[atom2_idx - num_atom]))
            rw_mol.AddBond(atom1_idx, num_atom, bond_orders[bond_type])

        return rw_mol


class TautomerEnumerator(MoleculeTransformer):
    def call(self, molecule: rdkit.Chem.Mol) -> Iterable[rdkit.Chem.Mol]:
        # return tautomer_enumerator.Enumerate(molecule)
        for mol in tautomer_enumerator.Enumerate(molecule):
            # Unfortunate to have to round-trip SMILES here, but appears otherwise the
            # valences aren't updated correctly
            yield rdkit.Chem.MolFromSmiles(rdkit.Chem.MolToSmiles(mol))


class TautomerCanonicalizer(MoleculeTransformer):
    def call(self, molecule: rdkit.Chem.Mol) -> Iterable[rdkit.Chem.Mol]:
        canonical = tautomer_enumerator.Canonicalize(molecule)
        rdkit.Chem.FindMolChiralCenters(canonical, includeUnassigned=True)
        yield canonical


class StereoEnumerator(MoleculeTransformer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.opts = StereoEnumerationOptions(unique=True)

    def call(self, molecule: rdkit.Chem.Mol) -> Iterable[rdkit.Chem.Mol]:
        # return EnumerateStereoisomers(molecule, options=self.opts)

        smiles_in = rdkit.Chem.MolToSmiles(molecule)
        for out in EnumerateStereoisomers(molecule, options=self.opts):

            smiles_out = rdkit.Chem.MolToSmiles(out)
            stereo_count = count_stereocenters(smiles_out)
            if stereo_count['atom_unassigned'] != 0:
                print(f'{smiles_in}: {smiles_out}')
            # if stereo_count['bond_unassigned'] == 0, f'{smiles_in}: {smiles_out}'

            yield out


class SAScoreFilter(MoleculeFilter):
    def __init__(self, sa_score_threshold: float, min_atoms: int = 1, **kwargs):
        super().__init__(**kwargs)
        self.min_atoms = min_atoms
        self.sa_score_threshold = sa_score_threshold

    def filter(self, molecule: rdkit.Chem.Mol) -> bool:
        if molecule.GetNumAtoms() >= self.min_atoms:
            return sascorer.calculateScore(molecule) <= self.sa_score_threshold
        return True


class EmbeddingFilter(MoleculeFilter):
    def filter(self, molecule: rdkit.Chem.Mol) -> bool:
        molH = Chem.AddHs(molecule)

        try:
            assert EmbedMolecule(molH, maxAttempts=30, randomSeed=42) >= 0
            return True

        except (AssertionError, RuntimeError):
            return False


class GdbFilter(MoleculeFilter):
    def filter(self, molecule: rdkit.Chem.Mol) -> bool:
        try:
            return check_all_filters(molecule)
        except Exception as ex:
            logger.warning(f"Issue with GDBFilter and molecule {Chem.MolToSmiles(molecule)}: {ex}")
            return False


def count_stereocenters(smiles: str) -> Dict:
    """ Returns a count of both assigned and unassigned stereocenters in the
    given molecule. Mainly used for testing """

    mol = rdkit.Chem.MolFromSmiles(smiles)
    rdkit.Chem.FindPotentialStereoBonds(mol)

    stereocenters = rdkit.Chem.FindMolChiralCenters(mol, includeUnassigned=True)
    stereobonds = [bond for bond in mol.GetBonds() if bond.GetStereo() is not
                   rdkit.Chem.rdchem.BondStereo.STEREONONE]

    atom_assigned = len([center for center in stereocenters if center[1] != '?'])
    atom_unassigned = len([center for center in stereocenters if center[1] == '?'])

    bond_assigned = len([bond for bond in stereobonds if bond.GetStereo() is not
                         rdkit.Chem.rdchem.BondStereo.STEREOANY])
    bond_unassigned = len([bond for bond in stereobonds if bond.GetStereo() is
                           rdkit.Chem.rdchem.BondStereo.STEREOANY])

    return {'atom_assigned': atom_assigned,
            'atom_unassigned': atom_unassigned,
            'bond_assigned': bond_assigned,
            'bond_unassigned': bond_unassigned}
