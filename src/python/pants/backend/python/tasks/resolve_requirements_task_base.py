# coding=utf-8
# Copyright 2017 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import os

from pex.interpreter import PythonInterpreter
from pex.pex import PEX
from pex.pex_builder import PEXBuilder

from pants.backend.python.python_requirement import PythonRequirement
from pants.backend.python.tasks.build_local_python_distributions import \
  BuildLocalPythonDistributions
from pants.backend.python.tasks.pex_build_util import (dump_requirement_libs, dump_requirements,
                                                       inject_synthetic_dist_requirements)
from pants.base.hash_utils import hash_all
from pants.invalidation.cache_manager import VersionedTargetSet
from pants.task.task import Task
from pants.util.dirutil import safe_concurrent_creation


class ResolveRequirementsTaskBase(Task):
  """Base class for tasks that resolve 3rd-party Python requirements.

  Creates an (unzipped) PEX on disk containing all the resolved requirements.
  This PEX can be merged with other PEXes to create a unified Python environment
  for running the relevant python code.
  """

  @classmethod
  def prepare(cls, options, round_manager):
    round_manager.require_data(PythonInterpreter)
    round_manager.require_data(BuildLocalPythonDistributions.PYTHON_DISTS)

  def resolve_requirements(self, req_libs, local_dist_targets=None):
    """Requirements resolution for PEX files.

    :param req_libs: A list of :class:`PythonRequirementLibrary` targets to resolve.
    :param local_dist_targets: A list of :class:`PythonDistribution` targets to resolve.
    :returns: a PEX containing target requirements and any specified python dist targets.
    """
    tgts = req_libs
    if local_dist_targets:
      tgts = req_libs + local_dist_targets
    with self.invalidated(tgts) as invalidation_check:
      # If there are no relevant targets, we still go through the motions of resolving
      # an empty set of requirements, to prevent downstream tasks from having to check
      # for this special case.
      if invalidation_check.all_vts:
        target_set_id = VersionedTargetSet.from_versioned_targets(
            invalidation_check.all_vts).cache_key.hash
      else:
        target_set_id = 'no_targets'

      interpreter = self.context.products.get_data(PythonInterpreter)
      path = os.path.realpath(os.path.join(self.workdir, str(interpreter.identity), target_set_id))
      # Note that we check for the existence of the directory, instead of for invalid_vts,
      # to cover the empty case.
      if not os.path.isdir(path):
        with safe_concurrent_creation(path) as safe_path:
          # Handle locally-built python distribution dependencies.
          built_dists = self.context.products.get_data(BuildLocalPythonDistributions.PYTHON_DISTS)
          if built_dists:
            req_libs = inject_synthetic_dist_requirements(
              self.context.build_graph, built_dists, ':'.join(2 * [target_set_id])
            ) + req_libs
          builder = PEXBuilder(path=safe_path, interpreter=interpreter, copy=True)
          dump_requirement_libs(builder, interpreter, req_libs, self.context.log)
          builder.freeze()
    return PEX(path, interpreter=interpreter)

  def resolve_requirement_strings(self, interpreter, requirement_strings):
    """Resolve a list of pip-style requirement strings."""
    requirement_strings = sorted(requirement_strings)
    if len(requirement_strings) == 0:
      req_strings_id = 'no_requirements'
    elif len(requirement_strings) == 1:
      req_strings_id = requirement_strings[0]
    else:
      req_strings_id = hash_all(requirement_strings)

    path = os.path.realpath(os.path.join(self.workdir, str(interpreter.identity), req_strings_id))
    if not os.path.isdir(path):
      reqs = [PythonRequirement(req_str) for req_str in requirement_strings]
      with safe_concurrent_creation(path) as safe_path:
        builder = PEXBuilder(path=safe_path, interpreter=interpreter, copy=True)
        dump_requirements(builder, interpreter, reqs, self.context.log)
        builder.freeze()
    return PEX(path, interpreter=interpreter)

  @classmethod
  def merge_pexes(cls, path, pex_info, interpreter, pexes, interpeter_constraints=None):
    """Generates a merged pex at path."""
    pex_paths = [pex.path() for pex in pexes if pex]
    if pex_paths:
      pex_info = pex_info.copy()
      pex_info.merge_pex_path(':'.join(pex_paths))

    with safe_concurrent_creation(path) as safe_path:
      builder = PEXBuilder(safe_path, interpreter, pex_info=pex_info)
      if interpeter_constraints:
        for constraint in interpeter_constraints:
          builder.add_interpreter_constraint(constraint)
      builder.freeze()
