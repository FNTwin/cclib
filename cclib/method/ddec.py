# -*- coding: utf-8 -*-
#
# Copyright (c) 2020, the cclib development team
#
# This file is part of cclib (http://cclib.github.io) and is distributed under
# the terms of the BSD 3-Clause License.

"""Calculation of DDEC charges based on data parsed by cclib."""
import copy
import random
import numpy
import logging
import math
import os
import sys

from cclib.method.calculationmethod import Method
from cclib.method.volume import electrondensity_spin
from cclib.parser.utils import convertor
from cclib.parser.utils import find_package


class MissingInputError(Exception):
    pass


class DDEC6(Method):
    """DDEC6 charges."""

    # All of these are required for DDEC6 charges.
    required_attrs = ("homos", "mocoeffs", "nbasis", "gbasis")

    def __init__(
        self, data, volume, proatom_path=None, progress=None, loglevel=logging.INFO, logname="Log"
    ):
        # Inputs are:
        # data -- ccData object that describe target molecule.
        # volume -- Volume object that describe target Cartesian grid.
        # proatom_path -- path to proatom densities
        #      (directory containing atoms.h5 in horton or c2_001_001_000_400_075.txt in chargemol)
        super(DDEC6, self).__init__(data, progress, loglevel, logname)

        self.volume = volume
        self.fragresults = None

        if numpy.sum(self.data.coreelectrons) != 0:
            # TODO: Pseudopotentials should be added back
            pass

        # Check whether proatom_path is a valid directory or not.
        assert os.path.isdir(
            proatom_path
        ), "Directory that contains proatom densities should be added as an input."
        
        # Read in reference charges.
        self.proatom_density = []
        self.radial_grid_r = []
        for atom_number in self.data.atomnos:
            density, r = self._read_proatom(proatom_path, atom_number, 0)
            self.proatom_density.append(density)
            self.radial_grid_r.append(r)

    def __str__(self):
        """Return a string representation of the object."""
        return "DDEC6 charges of {}".format(self.data)

    def __repr__(self):
        """Return a representation of the object."""
        return "DDEC6({})".format(self.data)

    def _check_required_attributes(self):
        super(DDEC6, self)._check_required_attributes()

    def _read_proatom(self, directory, atom_num, charge):
        """Return a list containing proatom reference densities."""
        # TODO: Treat calculations with psuedopotentials
        # TODO: Modify so that proatom densities are read only once for horton
        #       [https://github.com/cclib/cclib/pull/914#discussion_r464039991]
        # File name format:
        #   ** Chargemol **
        #       c2_[atom number]_[nuclear charge]_[electron count]_[cutoff radius]_[# shells]
        #   ** Horton **
        #       atoms.h5
        # File format:
        #   Starting from line 13, each line contains the charge densities for each shell
        chargemol_path = os.path.join(
            directory,
            "c2_{:03d}_{:03d}_{:03d}_500_100.txt".format(atom_num, atom_num, atom_num - charge),
        )
        horton_path = os.path.join(directory, "atoms.h5")

        if os.path.isfile(chargemol_path):
            # Use chargemol proatom densities
            # Each shell is 5.600570811644798 angstroms apart (uniform).
            # *scalefactor* = 10.58354497764173 in module_global_parameter.f08
            density = numpy.loadtxt(path, skiprows=12, dtype=float)
            radiusgrid = numpy.arange(1, len(density + 1)) * 5.600570811644798

        elif os.path.isfile(horton_path):
            # Use horton proatom densities
            assert find_package("h5py"), ("h5py is needed to read in proatom densities from horton.")

            import h5py

            with h5py.File(horton_path, "r") as proatomdb:
                keystring = "Z={}_Q={:+d}".format(atom_num, charge)

                # gridspec is specification of integration grid for proatom densities in horton.
                # Example -- ['PowerRTransform', '1.1774580743206259e-07', '20.140888089596444', '41']
                #   is constructed using PowerRTransform grid
                #   with rmin = 1.1774580743206259e-07
                #        rmax = 20.140888089596444
                #   and  ngrid = 41
                # PowerRTransform is default in horton-atomdb.py.
                gridtype, gridmin, gridmax, gridn = proatomdb[keystring].attrs["rtransform"].split()
                gridmin = convertor(float(gridmin), "bohr", "Angstrom")
                gridmax = convertor(float(gridmax), "bohr", "Angstrom")
                gridn = int(gridn)
                # Convert byte to string in Python3
                if sys.version[0] == '3':
                    gridtype = gridtype.decode('UTF-8')

                # First verify that it is one of recognized grids
                assert gridtype in ["LinearRTransform", "ExpRTransform", "PowerRTransform"], "Grid type not recognized."

                if gridtype == "LinearRTransform":
                    # Linear transformation. r(t) = rmin + t*(rmax - rmin)/(npoint - 1)
                    gridcoeff = (gridmax - gridmin) / (gridn - 1)
                    radiusgrid = gridmin + numpy.arange(1, gridn + 1) * gridcoeff
                elif gridtype == "ExpRTransform":
                    # Exponential transformation. r(t) = rmin*exp(t*log(rmax/rmin)/(npoint - 1))
                    gridcoeff = math.log(gridmax / gridmin) / (gridn - 1)
                    radiusgrid = gridmin * numpy.exp(numpy.arange(1, gridn + 1) * gridcoeff)
                elif gridtype == "PowerRTransform":
                    # Power transformation. r(t) = rmin*t^power
                    # with  power = log(rmax/rmin)/log(npoint)
                    gridcoeff = math.log(gridmax/gridmin) / math.log(gridn)
                    radiusgrid = gridmin * numpy.power(numpy.arange(1, gridn + 1), gridcoeff)

                density = list(proatomdb[keystring]["rho"])

                del h5py
        
        else:
            raise MissingInputError("Pro-atom densities were not found in the specified path.")
            
        return density, radiusgrid

    def calculate(self, indices=None, fupdate=0.05):
        """
        Calculate DDEC6 charges based on doi: 10.1039/c6ra04656h paper.
        Cartesian, uniformly spaced grids are assumed for this function.
        """

        # Obtain charge densities on the grid if it does not contain one.
        if not numpy.any(self.volume.data):
            self.logger.info("Calculating charge densities on the provided empty grid.")
            if len(self.data.mocoeffs) == 1:
                self.chgdensity = electrondensity_spin(
                    self.data, self.volume, [self.data.mocoeffs[0][: self.data.homos[0]]]
                )
                self.chgdensity.data *= 2
            else:
                self.chgdensity = electrondensity_spin(
                    self.data,
                    self.volume,
                    [
                        self.data.mocoeffs[0][: self.data.homos[0]],
                        self.data.mocoeffs[1][: self.data.homos[1]],
                    ],
                )
        # If charge densities are provided beforehand, log this information
        # `Volume` object does not contain (nor rely on) information about the constituent atoms.
        else:
            self.logger.info("Using charge densities from the provided Volume object.")
            self.chgdensity = self.volume