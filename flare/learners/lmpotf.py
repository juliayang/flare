from lammps import lammps
import ase, os, numpy as np, sys
from typing import Union, Optional, Callable, Any, List
from flare.bffs.sgp._C_flare import Structure, SparseGP
from flare.bffs.sgp.sparse_gp import optimize_hyperparameters
import logging


def transform_stress(stress: List[List[float]]) -> List[List[float]]:
    return -np.array(
        [
            stress[(0, 0)],
            stress[(0, 1)],
            stress[(0, 2)],
            stress[(1, 1)],
            stress[(1, 2)],
            stress[(2, 2)],
        ]
    )


class LMPOTF:
    """
    Module for performing On-The-Fly (OTF) training, also known as active learning,
    entirely within LAMMPS.

    Parameters
    ----------
    sparse_gp
        The :cpp:class:`SparseGP` object to train.
    descriptors
        A list of descriptor objects, or a single descriptor (most common), e.g. :cpp:class:`B2`.
    rcut
        The interaction cut-off radius.
    type2number
        The atomic numbers of all LAMMPS types.
    dftcalc
        An ASE calculator, e.g. Espresso.
    energy_correction
        Per-type correction to the DFT potential energy.
    dft_call_threshold
        Uncertainty threshold for whether to call DFT.
    dft_add_threshold
        Uncertainty threshold for whether to add an atom to the training set.
    std_xyz_fname
        Function for the name of the file in which to save ASE Atoms with per-atom uncertainties as charges.
        Takes as input this LMPOTF object and the current step.
    model_fname
        Name of the saved model, must correspond to `pair_coeff`.
    hyperparameter_optimization
        Boolean function that determines whether to run hyperparameter optimization, as a function of this LMPOTF
        object, the LAMMPS instance and the current step.
    opt_bounds
        Bounds for the hyperparameter optimization.
    opt_method
        Algorithm for the hyperparameter optimization.
    opt_iterations
        Max number of iterations for the hyperparameter optimization.
    post_dft_callback
        A function that is called after every DFT call. Receives this LMPOTF object and the current step.
    wandb
        The wandb object, which should already be initialized.
    log_fname
        An output file to which logging info is written.
    """

    def __init__(
        self,
        sparse_gp: SparseGP,
        descriptors: List,
        rcut: float,
        type2number: Union[(int, List[int])],
        dftcalc: object,
        energy_correction: List[float] = 0.0,
        force_training=True,
        energy_training=True,
        stress_training=True,
        dft_call_threshold: float = 0.005,
        dft_add_threshold: float = 0.0025,
        dft_xyz_fname=None,
        std_xyz_fname: Optional[Callable[([int], str)]] = None,
        model_fname: str = "otf.flare",
        hyperparameter_optimization: Callable[
            (["LMPOTF", object, int], bool)
        ] = lambda lmpotf, lmp, step: False,
        opt_bounds: Optional[List[float]] = None,
        opt_method: Optional[str] = "L-BFGS-B",
        opt_iterations: Optional[int] = 50,
        post_dft_callback: Callable[(["LMPOTF", int], None)] = lambda lmpotf, step: 0,
        wandb: object = None,
        log_fname: str = "otf.log",
    ) -> object:
        """

        """
        self.sparse_gp = sparse_gp
        self.descriptors = np.atleast_1d(descriptors)
        self.rcut = rcut
        self.type2number = np.atleast_1d(type2number)
        self.ntypes = len(self.type2number)
        self.energy_correction = np.atleast_1d(energy_correction)
        assert len(self.energy_correction) == self.ntypes
        self.dftcalc = dftcalc
        self.dft_call_threshold = dft_call_threshold
        self.dft_add_threshold = dft_add_threshold
        self.post_dft_callback = post_dft_callback
        self.force_training = force_training
        self.energy_training = energy_training
        self.stress_training = stress_training
        self.dft_calls = 0
        self.last_dft_call = -100
        self.dft_xyz_fname = dft_xyz_fname
        self.std_xyz_fname = std_xyz_fname
        self.model_fname = model_fname
        self.hyperparameter_optimization = hyperparameter_optimization
        self.opt_bounds = opt_bounds
        self.opt_method = opt_method
        self.opt_iterations = opt_iterations
        self.wandb = wandb
        logging.basicConfig(
            filename=log_fname, level=(logging.DEBUG), format="%(asctime)s: %(message)s"
        )
        self.logger = logging.getLogger("lmpotf")

    def save(self, fname):
        self.sparse_gp.write_mapping_coefficients(fname, "LMPOTF", 0)

    def step(self, lmpptr, evflag=0):
        """
        Function called by LAMMPS at every step.
        This is the function that must be called by `fix python/invoke`.

        Parameters
        ----------
        lmpptr : ptr
            Pointer to running LAMMPS instance.
        evflag : int
            evflag given by LAMMPS, ignored.
        """
        try:
            lmp = lammps(ptr=lmpptr)
            natoms = lmp.get_natoms()
            x = lmp.gather_atoms("x", 1, 3)
            x = np.ctypeslib.as_array(x, shape=(natoms, 3)).reshape(natoms, 3)
            step = int(lmp.get_thermo("step"))
            boxlo, boxhi, xy, yz, xz, _, _ = lmp.extract_box()
            cell = np.diag(np.array(boxhi) - np.array(boxlo))
            cell[(1, 0)] = xy
            cell[(2, 0)] = xz
            cell[(2, 1)] = yz
            types = lmp.gather_atoms("type", 0, 1)
            types = np.ctypeslib.as_array(types, shape=natoms)
            structure = Structure(cell, types - 1, x, self.rcut, self.descriptors)
            if self.dft_calls == 0:
                self.logger.info("Initial step, calling DFT")
                pe, F = self.run_dft(cell, x, types, step, structure)
                self.sparse_gp.add_training_structure(structure)
                self.sparse_gp.add_random_environments(structure, [16])
                self.sparse_gp.update_matrices_QR()
                self.save(self.model_fname)
            else:
                self.logger.info(f"Step {step}")
                sigma = self.sparse_gp.hyperparameters[0]
                self.sparse_gp.predict_local_uncertainties(structure)
                variances = structure.local_uncertainties[0]
                stds = np.sqrt(np.abs(variances)) / sigma
                if self.std_xyz_fname is not None:
                    frame = ase.Atoms(
                        positions=x,
                        numbers=(self.type2number[types - 1]),
                        cell=cell,
                        pbc=True,
                    )
                    frame.set_array("charges", stds)
                    ase.io.write((self.std_xyz_fname(step)), frame, format="extxyz")
                wandb_log = {"max_uncertainty": np.amax(stds)}
                self.logger.info(f"Max uncertainty: {np.amax(stds)}")
                call_dft = np.any(stds > self.dft_call_threshold)
                if call_dft:
                    self.sparse_gp.predict_DTC(structure)
                    predE = structure.mean_efs[0]
                    predF = structure.mean_efs[1:-6].reshape((-1, 3))
                    predS = structure.mean_efs[-6:]
                    Fstd = np.sqrt(np.abs(structure.variance_efs[1:-6])).reshape(
                        (-1, 3)
                    )
                    Estd = np.sqrt(np.abs(structure.variance_efs[0]))
                    Sstd = np.sqrt(np.abs(structure.variance_efs[-6:]))
                    wandb_log["max_F_uncertainty"] = np.amax(Fstd)
                    self.logger.info(f"Max force uncertainty: {np.amax(Fstd)}")
                    self.logger.info(f"DFT call #{self.dft_calls}")
                    pe, F = self.run_dft(cell, x, types, step, structure)
                    atoms_to_be_added = np.arange(natoms)[stds > self.dft_add_threshold]
                    self.sparse_gp.add_training_structure(structure)
                    self.sparse_gp.add_specific_environments(
                        structure, atoms_to_be_added
                    )
                    self.sparse_gp.update_matrices_QR()
                    if self.hyperparameter_optimization(self, lmp, step):
                        self.logger.info("Optimizing hyperparameters!")
                        self.sparse_gp.compute_likelihood_stable()
                        likelihood_before = self.sparse_gp.log_marginal_likelihood
                        optimize_hyperparameters(
                            (self.sparse_gp),
                            bounds=(self.opt_bounds),
                            method=(self.opt_method),
                            max_iterations=(self.opt_iterations),
                        )
                        likelihood_after = self.sparse_gp.log_marginal_likelihood
                        self.logger.info(
                            f"Likelihood before/after: {likelihood_before:.2e} {likelihood_after:.2e}"
                        )
                        self.logger.info(
                            f"Likelihood gradient: {self.sparse_gp.likelihood_gradient}"
                        )
                        self.logger.info(
                            f"Hyperparameters: {self.sparse_gp.hyperparameters}"
                        )
                    self.save(self.model_fname)
                    lmp.command(f"pair_coeff * * {self.model_fname}")
                    wandb_log["Fmae"] = np.mean(np.abs(F - predF))
                    wandb_log["Emae"] = np.abs(pe - predE) / natoms
                    wandb_log["n_added"] = len(atoms_to_be_added)
                    for qty in ("n_added", "Fmae", "Emae"):
                        self.logger.info(f"{qty}: {wandb_log[qty]}")

                if self.wandb is not None:
                    wandb_log["uncertainties"] = self.wandb.Histogram(stds)
                    wandb_log["Temp"] = lmp.get_thermo("temp")
                    wandb_log["Press"] = lmp.get_thermo("press")
                    wandb_log["PotEng"] = lmp.get_thermo("pe")
                    wandb_log["Vol"] = lmp.get_thermo("vol")
                    if call_dft:
                        wandb_log["Funcertainties"] = self.wandb.Histogram(Fstd.ravel())
                        wandb_log["Ferror"] = self.wandb.Histogram(
                            np.abs(F - predF).ravel()
                        )
                    self.wandb.log(wandb_log, step=step)
        except Exception as err:
            try:
                self.logger.exception("LMPOTF ERROR")
                raise err
            finally:
                err = None
                del err

    def run_dft(self, cell, x, types, step, structure):
        atomic_numbers = self.type2number[types - 1]
        frame = ase.Atoms(
            positions=x,
            numbers=atomic_numbers,
            cell=cell,
            calculator=(self.dftcalc),
            pbc=True,
        )
        pe = frame.get_potential_energy()
        pe -= np.sum(self.energy_correction[types - 1])
        F = frame.get_forces()
        stress = frame.get_stress(voigt=False)
        if self.dft_xyz_fname is not None:
            ase.io.write((self.dft_xyz_fname(step)), frame, format="extxyz")
        if self.force_training:
            structure.forces = F.reshape(-1)
        if self.energy_training:
            structure.energy = np.array([pe])
        if self.stress_training:
            structure.stresses = transform_stress(stress)
        self.dft_calls += 1
        self.last_dft_call = step
        self.post_dft_callback(self, step)
        return (pe, F)
