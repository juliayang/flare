import time, os, shutil, glob, subprocess
from copy import deepcopy
import pytest
flare_pp = pytest.importorskip("flare_pp")
import numpy as np
import yaml

from flare.io.otf_parser import OtfAnalysis
from flare.learners.otf import OTF
from flare.scripts.otf_train import fresh_start_otf, restart_otf
from ase.io import read, write

np.random.seed(12345)

md_list = ["Fake"]
number_of_steps = 5


@pytest.mark.parametrize("md_engine", md_list)
def test_otf_md(md_engine):
    for f in glob.glob(md_engine + "*"):
        if "_ckpt" not in f:
            os.remove(f)
        else:
            shutil.rmtree(f)

    # Modify the config for different MD engines 
    with open("../examples/test_SGP_Fake_fresh.yaml", "r") as f:
        config = yaml.safe_load(f)

    config["otf"]["output_name"] = md_engine

    print("fresh start")

    # Run OTF
    fresh_start_otf(config)

    print("done fresh start")

    # Check that the GP forces change.
    output_name = f"{md_engine}.out"
    otf_traj = OtfAnalysis(output_name)

    dft_traj = read("test_files/sic_dft.xyz", index=":")
    for i in range(number_of_steps - 1):
        assert np.allclose(dft_traj[i+1].positions, otf_traj.position_list[i], atol=1e-4), i
    #comp1 = otf_traj.force_list[0][1, 0]
    #comp2 = otf_traj.force_list[-1][1, 0]
    #assert (comp1 != comp2)


@pytest.mark.parametrize("md_engine", md_list)
def test_load_checkpoint(md_engine):

    with open("../examples/test_restart.yaml", "r") as f:
        config = yaml.safe_load(f)

    config["otf"]["checkpoint"] = md_engine + "_checkpt.json"
    restart_otf(config)

    for tmpfile in ["*.npy", "*.mgp", "*.var"]:
        for f in glob.glob(tmpfile):
            os.remove(f)

    for tmpdir in ["kv3*", "otf_data", "out", "mgp_grids"]:
        for f in glob.glob(tmpdir):
            shutil.rmtree(f, ignore_errors=True)


@pytest.mark.skipif(("PyLAMMPS" not in md_list) or ("VelocityVerlet" not in md_list), reason="md_list does not include both PyLAMMPS and VelocityVerlet")
def test_fakemd_match_gpfa():
    pytest.skip()

    lammps_traj = OtfAnalysis("PyLAMMPS.out")
    verlet_traj = OtfAnalysis("VelocityVerlet.out")
    pos1 = lammps_traj.position_list[0]
    pos2 = verlet_traj.position_list[0]
    cell1 = lammps_traj.cell_list[0]
    cell2 = verlet_traj.cell_list[0]

    # check the volumes are the same
    assert np.linalg.det(cell1) == np.linalg.det(cell2)

    # check the positions only differ by a multiple of cell
    pos_diff = (pos1 - pos2) @ np.linalg.inv(cell1)
    for i in np.reshape(pos_diff.round(4), -1):
        assert i.is_integer()


@pytest.mark.parametrize("md_engine", md_list)
def test_otf_parser(md_engine):
    pytest.skip()

    output_name = f"{md_engine}.out"
    otf_traj = OtfAnalysis(output_name)

    print("ase otf traj parsed")
    # Check that the GP forces change.
    comp1 = otf_traj.force_list[0][1, 0]
    comp2 = otf_traj.force_list[-1][1, 0]
#    assert (comp1 != comp2)

    for tmpdir in [md_engine + "*ckpt_*", "tmp"]:
        for f in glob.glob(tmpdir):
            shutil.rmtree(f)

    for tmpfile in ["*.flare", "log.*", "traj.xyz", "thermo.txt", md_engine + "*"]:
        for f in glob.glob(tmpfile):
            os.remove(f)
