import loopy as lp
import numpy as np
import pycuda.driver as cuda
from math import ceil, sqrt, floor
from pytools import memoize_method
from pycuda.compiler import SourceModule
from pyop2.utils import cached_property
from pytools import ImmutableRecord


# {{{ implementing the tiling transformation

class TilingConfiguration(ImmutableRecord):
    """
    Records the configuration for :func:`pyop2.gpu.tile.tiled_transform`.

    :attr ncells_per_block: Number of cells whose computation workload is to be
        given to one CUDA block.
    :attr nthreads_per_cell: Number of CUDA threads to be launched for one each
        cell in the mesh.
    :attr matvec1_row_tile_length: Number of rows in the tile of the first
        matvec (first matvec := quadrature stage)
    :attr matvec1_col_tile_length: Number of columns in the tile of the first
        matvec (first matvec := quadrature stage)
    :attr matvec2_row_tile_length: Number of rows in the tile of the second
        matvec (second matvec := output DoF stage)
    :attr matvec2_col_tile_length: Number of columns in the tile of the second
        matvec (second matvec := output DoF stage)
    :attr load_coordinates_to_shared: Should the coordinates of the cell be
        prefeteched to shared memory?
    :attr load_input_to_shared: Should the input DoFs be prefetched to shared
        memory?
    :attr load_mats_to_shared: Should the local FEM operator matrices be loaded
        to shared memory?
    :attr load_quad_weights_to_shared: Should the quadrature weigts be loaded
        to shared memory?
    :attr tiled_prefetch_of_inputs: If input DoFs are prefetched to shared
        memory, should they be prefetched in tile lengths?
    :attr tiled_prefetch_of_quad_weights: If the quadrature weights are
        prefethced to shared memory, should they in prefetched in tile lengths?
    """
    def __init__(self,
            ncells_per_block,
            nthreads_per_cell,
            t1_r, t1_c,
            t2_r, t2_c,
            load_coordinates_to_shared,
            load_input_to_shared,
            load_mats_to_shared,
            load_quad_weights_to_shared,
            tiled_prefetch_of_inputs,
            tiled_prefetch_of_quad_weights):
        super(TilingConfiguration, self).__init__(
                ncells_per_block=ncells_per_block,
                nthreads_per_cell=nthreads_per_cell,
                matvec1_row_tile_length=t1_r,
                matvec1_col_tile_length=t1_c,
                matvec2_row_tile_length=t2_r,
                matvec2_col_tile_length=t2_c,
                load_coordinates_to_shared=load_coordinates_to_shared,
                load_input_to_shared=load_input_to_shared,
                load_mats_to_shared=load_mats_to_shared,
                load_quad_weights_to_shared=load_quad_weights_to_shared,
                tiled_prefetch_of_inputs=tiled_prefetch_of_inputs,
                tiled_prefetch_of_quad_weights=tiled_prefetch_of_quad_weights)


def _make_tv_array_arg(tv):
    assert tv.address_space != lp.AddressSpace.PRIVATE
    arg = lp.ArrayArg(
            name=tv.name,
            dtype=tv.dtype,
            shape=tv.shape,
            dim_tags=tv.dim_tags,
            offset=tv.offset,
            dim_names=tv.dim_names,
            order=tv.order,
            alignment=tv.alignment,
            address_space=tv.address_space,
            is_output_only=not tv.read_only)
    return arg


class KernelMetadata(ImmutableRecord):
    def __init__(self, **kwargs):
        assert isinstance(kwargs['quad_iname'], str)
        assert isinstance(kwargs['inputDoFs'], list)
        assert isinstance(kwargs['coords'], str)
        assert isinstance(kwargs['outputDoF'], str)
        assert isinstance(kwargs['outputDoF_init_iname'], str)
        assert isinstance(kwargs['quad_iname_in_quad_stage'], str)
        assert isinstance(kwargs['quad_iname_in_basis_stage'], str)
        assert isinstance(kwargs['doF_inames_in_quad_stage'], list)
        assert isinstance(kwargs['doF_iname_in_basis_stage'], str)
        assert isinstance(kwargs['scatter_iname'], str)
        assert isinstance(kwargs['nquad'], int)
        assert isinstance(kwargs['n_inputDoFs'], list)
        assert isinstance(kwargs['n_outputDoF'], int)
        # FIXME: non-obvious styling
        # just choose the lengthy route
        assert len(kwargs) == 13
        super(KernelMetadata, self).__init__(**kwargs)


def work_which_should_be_done_by_passing_metadata(kernel):
    from pymbolic.primitives import Variable

    # quad iname
    # Logic: assumes that there is only one iname responsible for the
    # quadrature. TPEs do not fit within this model
    quad_iname, = [iname for iname in kernel.all_inames() if iname.startswith('form_ip')]

    # inputDof_x_outputDofs_x_coords: A set containing the variable names for the
    # temporaries of inputDofs, outputDofs and the coordinates.
    inputDof_x_outputDof_x_coords = set().union(*(insn.write_dependency_names() for
            insn in kernel.instructions if 'gather' in insn.tags)) - kernel.all_inames()

    # outputDof names
    # Logic: The only temporaries which are written during basis stage tagged
    # by TSFC.
    outputDoF = set()
    for insn in kernel.instructions:
        if 'basis' in insn.tags:
            outputDoF.update(insn.write_dependency_names()-kernel.all_inames())

    outputDoF, = outputDoF

    # coords name
    # Logic: assumming that the coordinate transformation is affine i.e. one
    # Jacobian computation for each cell.
    coords = set()
    for insn in kernel.instructions:
        if 'quadrature' in insn.tags and (insn.within_inames == frozenset(["n"])):
            coords = coords | (insn.read_dependency_names() &
                    inputDof_x_outputDof_x_coords)

    coords, = coords

    # inputDof names
    inputDoFs = list(inputDof_x_outputDof_x_coords - frozenset([coords, outputDoF]))

    # 1. Figure out the input basis iname

    # {{{ scatter iname

    # Logic: Assumes that there is only DoFs of one component of the basis
    # functions computed per kernel i.e. one component in a mixed FEM setting.

    scatter_insn, = [insn for insn in kernel.instructions if 'scatter' in
            insn.tags]
    scatter_map = scatter_insn.assignee.index_tuple[0]
    scatter_iname, = set(scatter_map.index_tuple) - set([Variable('n')])
    scatter_iname = scatter_iname.name

    # }}}

    # {{{ basis init iname

    # iname over the DoFs for the input variable inputDofs[i]

    outputDoF_init_iname, = [insn.assignee.index_tuple[1].name for insn in
            kernel.instructions if ('gather' in insn.tags) and (outputDoF in
                insn.write_dependency_names())]

    # }}}

    # Assumption: only one variable for the outputDoF supported.
    (doF_iname_in_basis_stage,), = set([insn.within_inames - frozenset(['n', quad_iname]) for insn in kernel.instructions if 'basis' in insn.tags])

    # Assumption: All such inames match: 'form_i(?_([0-9]+))'
    doF_inames_in_quad_stage = set([iname.startswith('form_i') for iname in kernel.all_inames()])

    # {{{ tagging the stages of the kernel

    #TODO: Should be interpreted in TSFC

    new_insns = []

    done_with_jacobi_eval = False
    done_with_quad_init = False
    done_with_quad_reduction = False
    done_with_quad_wrap_up = False
    done_with_basis_reduction = False

    for insn in kernel.instructions:
        if not done_with_jacobi_eval:
            if quad_iname in insn.within_inames:
                done_with_jacobi_eval = True

            else:
                new_insns.append(insn.copy(tags=insn.tags
                    | frozenset(["jacobi_eval"])))
                continue
        if not done_with_quad_init:
            if doF_inames_in_quad_stage & insn.within_inames:
                done_with_quad_init = True
            else:
                new_insns.append(insn.copy(tags=insn.tags
                    | frozenset(["quad_init"])))
                continue
        if not done_with_quad_reduction:
            if doF_inames_in_quad_stage & insn.within_inames:
                new_insns.append(insn.copy(tags=insn.tags
                    | frozenset(["quad_redn"])))
                continue
            else:
                done_with_quad_reduction = True
        if not done_with_quad_wrap_up:
            if 'basis' in insn.tags:
                done_with_quad_wrap_up = True
            else:
                new_insns.append(insn.copy(tags=insn.tags
                    | frozenset(["quad_wrap_up"])))
                continue
        if not done_with_basis_reduction:
            if quad_iname not in insn.within_inames:
                done_with_basis_reduction = True
            else:
                new_insns.append(insn.copy(tags=insn.tags
                    | frozenset(["basis_redn"])))
                continue
        new_insns.append(insn)

    assert done_with_basis_reduction

    kernel = kernel.copy(instructions=new_insns)

    # }}}

    # {{{ reading info about the finite element

    nquad = int(lp.symbolic.pw_aff_to_expr(
            kernel.get_iname_bounds(quad_iname, constants_only=True).size))
    n_inputDoFs = [int(lp.symbolic.pw_aff_to_expr(
        kernel.get_iname_bounds(iname, constants_only=True).size)) for iname in
        doF_inames_in_quad_stage]
    n_outputDoF = int(lp.symbolic.pw_aff_to_expr(
            kernel.get_iname_bounds(outputDoF,
                constants_only=True).size))

    # }}}

    return kernel, KernelMetadata(
            quad_iname=quad_iname,
            inputDoFs=inputDoFs,
            coords=coords,
            outputDoF=outputDoF,
            quad_iname_in_basis_stage=quad_iname+'_basis',
            quad_iname_in_quad_stage=quad_iname+'_quad',
            doF_inames_in_quad_stage=doF_inames_in_quad_stage,
            outputDoF_init_iname=outputDoF_init_iname,
            scatter_iname=scatter_iname,
            doF_iname_in_basis_stage=doF_iname_in_basis_stage,
            nquad=nquad,
            n_inputDoFs=n_inputDoFs,
            n_outputDoF=n_outputDoF
            )


def tiled_transform(kernel, callables_table, tiling_config):
    """
    :param tiling_config: An instance of :class:`pyop2.gpu.tiling_config
    """
    assert isinstance(tiling_config, TilingConfiguration)

    nc = tiling_config.ncells_per_block
    nt = tiling_config.nthreads_per_cell
    t1_r, t1_c = tiling_config.matvec1_row_tile_length, tiling_config.matvec1_col_tile_length
    t2_r, t2_c = tiling_config.matvec2_row_tile_length, tiling_config.matvec2_col_tile_length

    # {{{ Inferring variables

    kernel, metadata = work_which_should_be_done_by_passing_metadata(kernel)
    quad_iname = metadata.quad_iname
    inputDoFs = metadata.inputDoFs
    coords = metadata.coords
    outputDoF = metadata.outputDoF
    quad_iname_in_basis_stage = metadata.quad_iname_in_basis_stage
    quad_iname_in_quad_stage = metadata.quad_iname_in_quad_stage
    doF_inames_in_quad_stage = metadata.doF_inames_in_quad_stage
    outputDoF_init_iname = metadata.outputDoF_init_iname
    scatter_iname = metadata.scatter_iname
    doF_iname_in_basis_stage = metadata.doF_iname_in_basis_stage

    # }}}

    # {{{ privatize temps for function evals and make them LOCAL

    #FIXME: Need these variables from TSFC's metadata
    evaluation_variables = (set().union(*[insn.write_dependency_names() for insn in kernel.instructions if 'quad_wrap_up' in insn.tags])
            & set().union(*[insn.read_dependency_names() for insn in kernel.instructions if 'basis' in insn.tags]))

    kernel = lp.privatize_temporaries_with_inames(kernel, quad_iname,
            evaluation_variables)
    new_temps = kernel.temporary_variables.copy()
    for eval_var in evaluation_variables:
        new_temps[eval_var] = new_temps[eval_var].copy(
                address_space=lp.AddressSpace.LOCAL)
    kernel = kernel.copy(temporary_variables=new_temps)

    # Duplicate inames to separate transformation logic for quadrature and basis part
    kernel = lp.duplicate_inames(kernel, quad_iname, "tag:quadrature",
            quad_iname_in_quad_stage)
    kernel = lp.duplicate_inames(kernel, quad_iname, "tag:basis",
            quad_iname_in_basis_stage)

    # }}}

    # {{{ change address space of constants to '__global'

    old_temps = kernel.temporary_variables.copy()
    args_to_make_global = [tv.initializer.flatten() for tv in old_temps.values() if tv.initializer is not None]

    new_temps = dict((tv.name, tv) for tv in old_temps.values() if tv.initializer is None)
    kernel = kernel.copy(
            args=kernel.args+[_make_tv_array_arg(tv) for tv in old_temps.values() if tv.initializer is not None],
            temporary_variables=new_temps)

    # }}}

    from loopy.loop import fuse_loop_domains
    kernel = fuse_loop_domains(kernel)

    from loopy.transform.data import remove_unused_axes_in_temporaries
    kernel = remove_unused_axes_in_temporaries(kernel)

    # {{{ remove noops

    noop_insns = set([insn.id for insn in kernel.instructions if
            isinstance(insn, lp.NoOpInstruction)])
    kernel = lp.remove_instructions(kernel, noop_insns)

    # }}}

    # Realize CUDA blocks
    kernel = lp.split_iname(kernel, "n", nc,
            outer_iname="iblock", inner_iname="icell")

    kernel = lp.privatize_temporaries_with_inames(kernel, 'icell',
            only_var_names=evaluation_variables)
    # from loopy.transform.batch import save_temporaries_in_loop
    # kernel = save_temporaries_in_loop(kernel, 'icell',
    #         evaluation_variables)

    # cut down the size of the number of basis coeffs written by each
    # thread(if there are multiple threads)
    kernel = lp.rename_iname(kernel, scatter_iname,
            doF_iname_in_basis_stage, True)
    kernel = lp.rename_iname(kernel, input_basis_coeff_temp,
            basis_iname_in_basis_redn, True)

    from loopy.transform.instruction import remove_unnecessary_deps
    kernel = remove_unnecessary_deps(kernel)

    from loopy.transform.make_scalar import remove_axis
    kernel = remove_axis(kernel, output_basis_coeff_temp, 0)

    kernel = lp.add_dependency(kernel,
            'writes:{}'.format(output_basis_coeff_temp),
            'tag:quad_wrap_up')

    if tiling_config.load_coordinates_to_shared:
        # FIXME: This configuration parameter seems unnecessary as of now. I
        # might choose not to support it.
        kernel = lp.privatize_temporaries_with_inames(kernel, 'icell',
                [coords_temp])
        kernel = lp.assignment_to_subst(kernel, coords_temp)
        raise NotImplementedError("This might be only useful for high order"
                " meshes.")

    # Splitting for tiles in matvec1
    kernel = lp.split_iname(kernel, quad_iname_in_quad_stage, t1_r, outer_iname='irowtile_matvec1')
    kernel = lp.split_iname(kernel, basis_iname_in_quad_redn, t1_c, outer_iname='icoltile_matvec1')

    # Splitting for tiles in matvec2
    kernel = lp.split_iname(kernel, basis_iname_in_basis_redn, t2_r, outer_iname='irowtile_matvec2')
    kernel = lp.split_iname(kernel, quad_iname_in_basis_stage, t2_c, outer_iname='icoltile_matvec2')

    # {{{ Prefetch wizardry

    if tiling_config.load_input_to_shared:
        kernel = lp.privatize_temporaries_with_inames(kernel, 'icell',
                only_var_names=[input_basis_coeff_temp])
        from loopy.transform.precompute import precompute_for_single_kernel
        # kernel = save_temporaries_in_loop(kernel, 'icell',
        #         [input_basis_coeff_temp])
        kernel = lp.assignment_to_subst(kernel, input_basis_coeff_temp)
        input_prcmpt_iname = 'input_basis_prcmpt'
        if tiling_config.tiled_prefetch_of_inputs:
            sweep_inames = (basis_iname_in_quad_redn+'_inner', 'icell')
            outer_inames = 'iblock,icoltile_matvec1,irowtile_matvec1'
        else:
            sweep_inames = ('icoltile_matvec1', basis_iname_in_quad_redn+'_inner', 'icell')
            outer_inames = 'iblock'
        kernel = precompute_for_single_kernel(kernel, callables_table,
                subst_use=input_basis_coeff_subst,
                sweep_inames=sweep_inames,
                precompute_outer_inames=outer_inames,
                precompute_inames=(input_prcmpt_iname, 'icell'),
                temporary_address_space=lp.AddressSpace.LOCAL,
                default_tag=None,
                )
        kernel = lp.split_iname(kernel, input_prcmpt_iname,
                nt, inner_tag="l.0")

    if tiling_config.load_mats_to_shared:
        from loopy.transform.data import add_prefetch_for_single_kernel
        #FIXME: Assuming that in all the constants the one with single axis is
        # the one corresponding to quadrature weights. fix it by passing some
        # metadata from TSFC.
        # FIXME: Sweep inames depends on the parallelization strategies for
        # both the matvecs, that needs to be taken care of.
        const_matrices_names = set([tv.name for tv in old_temps.values() if tv.initializer is not None and len(tv.shape) > 1])

        vng = kernel.get_var_name_generator()
        ing = kernel.get_instruction_id_generator()

        # {{{ Prefetching: QUAD PART

        quad_const_matrices = const_matrices_names & frozenset().union(*[insn.read_dependency_names() for insn in
            kernel.instructions if 'quad_redn' in insn.tags])
        sweep_inames = (quad_iname_in_quad_stage+'_inner',
                basis_iname_in_quad_redn+'_inner')
        fetch_outer_inames = 'iblock,icoltile_matvec1,irowtile_matvec1'

        quad_prefetch_insns = []

        quad_temp_names = [vng('quad_cnst_mtrix_prftch') for _ in quad_const_matrices]
        prefetch_inames = [vng("iprftch") for _ in range(2)]
        for temp_name, var_name in zip(quad_temp_names, quad_const_matrices):
            quad_prefetch_insns.append(ing("quad_prftch_insn"))

            kernel = add_prefetch_for_single_kernel(kernel, callables_table,
                    var_name=var_name,
                    sweep_inames=sweep_inames,
                    temporary_address_space=lp.AddressSpace.LOCAL,
                    dim_arg_names=prefetch_inames,
                    temporary_name=temp_name,
                    compute_insn_id=quad_prefetch_insns[-1],
                    fetch_outer_inames=fetch_outer_inames,
                    default_tag=None,
                    within="tag:quad_redn")

        kernel = lp.join_inames(kernel, prefetch_inames,
                new_iname='quad_prftch_iname')
        kernel = lp.split_iname(kernel, 'quad_prftch_iname',
                nc*nt, outer_tag="ilp")
        kernel = lp.split_iname(kernel, 'quad_prftch_iname_inner',
                nt, inner_tag='l.0', outer_tag='l.1')

        # }}}

        # {{{ Prefetching: BASIS PART

        basis_const_matrices = const_matrices_names & frozenset().union(*[insn.read_dependency_names() for insn in
            kernel.instructions if 'basis_redn' in insn.tags])
        basis_temp_names = [vng('basis_cnst_mtrix_prftch') for _ in basis_const_matrices]

        sweep_inames = (basis_iname_in_basis_redn+'_inner',
                quad_iname_in_basis_stage+'_inner')
        fetch_outer_inames = 'iblock,icoltile_matvec2,irowtile_matvec2'

        basis_prefetch_insns = []
        prefetch_inames = [vng("iprftch") for _ in range(2)]
        for temp_name, var_name in zip(basis_temp_names, basis_const_matrices):
            basis_prefetch_insns.append(ing("basis_prftch_insn"))

            kernel = add_prefetch_for_single_kernel(kernel, callables_table,
                    var_name=var_name,
                    sweep_inames=sweep_inames,
                    temporary_address_space=lp.AddressSpace.LOCAL,
                    dim_arg_names=prefetch_inames,
                    temporary_name=temp_name,
                    compute_insn_id=basis_prefetch_insns[-1],
                    fetch_outer_inames=fetch_outer_inames,
                    default_tag=None,
                    within="tag:basis_redn")

        kernel = lp.join_inames(kernel, prefetch_inames,
                new_iname='basis_prftch_iname')
        kernel = lp.split_iname(kernel, 'basis_prftch_iname',
                nc*nt, outer_tag="ilp")
        kernel = lp.split_iname(kernel, 'basis_prftch_iname_inner',
                nt, inner_tag='l.0', outer_tag='l.1')

        # }}}

        # {{{ using the same variable for both the prefetch shared mems

        if True:
            # TODO: Temporary transformation path until
            # https://gitlab.tiker.net/inducer/loopy/issues/205 is resolved.
            from loopy.transform.data import flatten_variable, absorb_temporary_into
            for var_name in quad_temp_names+basis_temp_names:
                kernel = flatten_variable(kernel, var_name)
            for quad_temp_name, basis_temp_name in zip(quad_temp_names,
                    basis_temp_names):
                if (t1_r*t1_c <= t2_r*t2_c):
                    kernel = absorb_temporary_into(kernel, basis_temp_name, quad_temp_name)
                else:
                    kernel = absorb_temporary_into(kernel, quad_temp_name, basis_temp_name)
        else:
            for quad_temp_name, basis_temp_name in zip(quad_temp_names,
                    basis_temp_names):
                kernel = lp.alias_temporaries(kernel, (quad_temp_name,
                    basis_temp_name), synchronize_for_exclusive_use=False)

        # }}}

        # {{{ Adding dependency between the prefetch instructions

        kernel = lp.add_dependency(kernel,
                " or ".join("id:{}".format(insn_id) for insn_id in
                    basis_prefetch_insns), "tag:quadrature")

        kernel = lp.add_dependency(kernel, 'tag:quad_redn', 'id:quad_prftch_insn*')
        kernel = lp.add_dependency(kernel, 'tag:basis_redn', 'id:basis_prftch_insn*')

        # }}}

        # do not enforce any dependency between the basis reductions and the
        # quadrature reductions.

        kernel = lp.remove_dependency(kernel, 'tag:quad_redn', 'tag:quad_redn')
        kernel = lp.remove_dependency(kernel, 'tag:basis_redn', 'tag:basis_redn')
        kernel = lp.add_dependency(kernel, 'tag:quad_wrap_up', 'tag:quad_redn')

    # }}}

    # {{{ Prefetch: Quad Weights

    if tiling_config.load_quad_weights_to_shared:
        from loopy.transform.data import add_prefetch_for_single_kernel
        quad_weights, = [tv.name for tv in old_temps.values() if tv.initializer is not None and len(tv.shape) == 1]
        vng = kernel.get_var_name_generator()
        ing = kernel.get_instruction_id_generator()
        quad_weight_prefetch_insn = ing("quad_wt_prftch_insn")
        quad_weight_prefetch_iname = vng("iprtftch")

        if tiling_config.tiled_prefetch_of_quad_weights:
            sweep_inames = (quad_iname_in_quad_stage+'_inner')
            fetch_outer_inames = 'irowtile_matvec1, iblock'
        else:
            sweep_inames = ('irowtile_matvec1', quad_iname_in_quad_stage+'_inner',)
            fetch_outer_inames = 'iblock'

        kernel = add_prefetch_for_single_kernel(kernel, callables_table,
                var_name=quad_weights,
                sweep_inames=sweep_inames,
                temporary_address_space=lp.AddressSpace.LOCAL,
                dim_arg_names=(quad_weight_prefetch_iname,),
                temporary_name='cnst_quad_weight_prftch',
                compute_insn_id=quad_weight_prefetch_insn,
                fetch_outer_inames=fetch_outer_inames,
                default_tag=None,
                within="tag:quad_wrap_up")

        kernel = lp.split_iname(kernel, quad_weight_prefetch_iname,
                nc * nt, outer_tag="ilp")
        kernel = lp.split_iname(kernel, quad_weight_prefetch_iname+'_inner',
                nt,
                outer_tag="l.1", inner_tag="l.0")

    # }}}

    # {{{ divide matvec1-tile's work across threads

    kernel = lp.split_iname(kernel, quad_iname_in_quad_stage+'_inner',
            nt, inner_tag="l.0", outer_tag="ilp")

    # }}}

    # {{{ divide matvec2-tile's work across threads

    kernel = lp.split_iname(kernel, basis_iname_in_basis_redn+'_inner',
            nt, inner_tag="l.0", outer_tag="ilp")

    # }}}

    # not sure what this condition would be for multiple inputs.
    # let it break for now.
    raise NotImplementedError("Developer wants to focus on other features now.."
            " hahahaaha. sob sob sob")
    if t1_c < nbasis:
        only_var_names = [insn.assignee.name for insn in kernel.instructions if
                'quad_init' in insn.tags]
        kernel = lp.privatize_temporaries_with_inames(kernel,
                quad_iname_in_quad_stage+'_inner_outer',
                only_var_names=only_var_names)
        kernel = lp.duplicate_inames(kernel,
                [quad_iname_in_quad_stage+'_inner_outer', ],
                within='tag:quad_wrap_up')
        kernel = lp.duplicate_inames(kernel,
                [quad_iname_in_quad_stage+'_inner_outer'],
                'tag:quad_init')
    else:
        kernel = lp.add_inames_to_insn(kernel, 'icoltile_matvec1', 'tag:quad_wrap_up or tag:quad_init')

    # before this point 't2' should be made a scalar.

    if t2_c < nquad:
        kernel = lp.privatize_temporaries_with_inames(kernel,
                basis_iname_in_basis_redn+'_inner_outer',
                only_var_names=[output_basis_coeff_temp])
        kernel = lp.duplicate_inames(kernel, [basis_iname_in_basis_redn+'_inner_outer'], within='tag:scatter')
        kernel = lp.duplicate_inames(kernel,
                [basis_iname_in_basis_redn+'_inner_outer'],
                within='tag:gather and writes:{}'.format(output_basis_coeff_temp))
    else:
        kernel = lp.add_inames_to_insn(kernel, 'icoltile_matvec2',
                'tag:scatter or (tag:gather and writes:{})'.format(output_basis_coeff_temp))

    # {{{ micro-optimizations

    if nt == 1 and not tiling_config.load_mats_to_shared:
        # FIXME: not general enough!
        raise RuntimeError()
        #@TODO: form_insn_19 and form_insn20 aren't general enough!
        kernel = lp.add_nosync(kernel, "local", "id:form_insn_19 or id:form_insn_20",
                "id:form_insn_21")

    # }}}

    kernel = lp.tag_inames(kernel, "icell:l.1, iblock:g.0")

    kernel = lp.remove_unused_inames(kernel)
    kernel = kernel.copy(loop_priority=frozenset())

    return kernel, args_to_make_global

# }}}


# {{{ auto tile

WARP_SIZE = 32


class AutoTiler:
    """
    Helper class to tune the :class:`pyop2.gpu.tile.TilingConfiguration` for
    :func:`pyop2.gpu.tile.tiled_transform`.

    :attr fem_program: An instance of :class:`loopy.program.Program` which is
        the FEM computational kernel to be tuned.

    See the entrypoint :func:`pyop2.gpu.tile.Autotiler.__call__`
    """
    def __init__(self, fem_program, num_candidate_knls):
        self.fem_program = fem_program
        self.num_candidate_knls = num_candidate_knls

    @cached_property
    def nbasis(self):
        return int(lp.symbolic.pw_aff_to_expr(
            self.fem_program.root_kernel.get_iname_bounds('form_i',
                constants_only=True).size))

    @cached_property
    def nquad(self):
        return int(lp.symbolic.pw_aff_to_expr(
                self.fem_program.root_kernel.get_iname_bounds('form_ip',
                    constants_only=True).size))

    @cached_property
    def num_const_matrices(self):
        """
        Returns the number of constant matrices in the FEM kernel.
        """
        const_matrices_in_quad = set()
        const_matrices_in_basis = set()
        const_matrices = frozenset([tv.name for tv in
            self.fem_program.root_kernel.temporary_variables.values() if
            tv.initializer is not None and len(tv.initializer.shape) == 2])

        for insn in self.fem_program.root_kernel.instructions:
            if 'quadrature' in insn.tags:
                const_matrices_in_quad.update(insn.read_dependency_names() &
                        const_matrices)
            if 'basis' in insn.tags:
                const_matrices_in_basis.update(insn.read_dependency_names() &
                        const_matrices)

        return max(len(const_matrices_in_quad), len(const_matrices_in_basis))

    @cached_property
    def num_func_eval_vars(self):
        """
        Returns the number of variables evaluated at the quadrature nodes.
        """
        evaluation_variables = (set().union(*[insn.write_dependency_names() for
            insn in self.fem_program.root_kernel.instructions if 'quadrature' in insn.tags]) &
            set().union(*[insn.read_dependency_names() for insn in
                self.fem_program.root_kernel.instructions if 'basis' in insn.tags]))

        return len(evaluation_variables)

    def get_local_barriers(self, t1_r, t1_c, t2_r, t2_c):
        """
        Returns the number of block level synchronization instructions in a
        single kernel execution.
        """
        return (
                ceil(self.nquad/t1_r) * ceil(self.nbasis/t1_c)
                + ceil(self.nbasis/t2_r) * ceil(self.nquad/t2_c))

    def theoretical_warps_per_sm(self, tiling_config):
        """
        Returns the number of warps residing on an Streaming Multiprocessor.
        """

        cells_per_block = tiling_config.ncells_per_block
        threads_per_cell = tiling_config.nthreads_per_cell
        t1_r, t1_c = tiling_config.matvec1_row_tile_length, tiling_config.matvec1_col_tile_length
        t2_r, t2_c = tiling_config.matvec2_row_tile_length, tiling_config.matvec2_col_tile_length

        # {{{ computing shared mem usage per block

        shared_usage = (
                self.num_const_matrices*max(t1_r*t1_c, t2_r*t2_c)
                + self.nquad
                + self.num_func_eval_vars*self.nquad*cells_per_block
                )

        # convert doubles to KB
        shared_usage *= 8e-3

        # }}}

        warps_per_block = floor((threads_per_cell*cells_per_block)/32)
        blocks_per_sm = min(96//shared_usage if shared_usage < 48 else 0, 32)
        warps_per_sm = blocks_per_sm*warps_per_block

        return warps_per_sm

    def get_work_efficiency(self, tiling_config):
        """
        Returns the efficieny(as a fraction) for a tile defined by t1_r x t1_c,
        t2_r x t2_c.

        One reason for inefficiency is if the number of threads in a CUDA block
        aren't a multiple of the warp size.
        """
        cells_per_block = tiling_config.ncells_per_block
        threads_per_cell = tiling_config.nthreads_per_cell
        t1_r = tiling_config.matvec1_row_tile_length
        t2_r = tiling_config.matvec2_row_tile_length

        # wasted work in the function evaluation stage
        wasted_work = self.nbasis*(
                (t1_r % threads_per_cell)*(self.nquad//t1_r)
                + ((self.nquad % t1_r) % threads_per_cell))

        wasted_work += self.nquad*(
                (t2_r % threads_per_cell)*(self.nbasis//t2_r)
                + ((self.nbasis % t2_r) % threads_per_cell))

        wasted_work_fraction = wasted_work / (2*self.nquad*self.nbasis)

        threads_in_block = threads_per_cell * cells_per_block
        warp_mismatch_factor = threads_in_block / (
                threads_in_block + (WARP_SIZE - (threads_in_block % WARP_SIZE)))

        return warp_mismatch_factor*(1-wasted_work_fraction)

    def actual_warps_per_sm(self, tiling_config):
        """
        Returns "actual warps residing per SM" = Efficiency * "theoretical
        warps reising per SM".
        """
        return (
                self.theoretical_warps_per_sm(tiling_config)
                * self.get_work_efficiency(tiling_config))

    @memoize_method
    def estimated_exec_time(self, tiling_config):
        """
        Returns a metric proportional to the execution time for a
        configuration.
        """

        n_c = tiling_config.ncells_per_block
        n_t = tiling_config.nthreads_per_cell
        t1_r, t1_c = tiling_config.matvec1_row_tile_length, tiling_config.matvec1_col_tile_length
        t2_r, t2_c = tiling_config.matvec2_row_tile_length, tiling_config.matvec2_col_tile_length
        n_w = self.actual_warps_per_sm(tiling_config)

        if n_w == 0:
            return float("inf")
        n_lb = self.get_local_barriers(t1_r, t1_c, t2_r, t2_c)
        n_blocks = (n_w * 32)/(n_t*n_c)

        # nb, nq = self.nbasis, self.nquad
        # return (n_t*nb + nb*nq/(n_t*n_c) + nb*nq*(n_t+n_c)/20.0)/n_w
        return n_lb/n_blocks

    def get_candiate_configs(self):

        threads_to_cells = {
                9: (7, ),
                8: (4, 8, 16),
                7: (9, ),
                4: (8, 16),
                3: (21, ),
                2: (16, 32, 64),
                1: (32, 64),
                }

        tiles = []

        for i in range(1, ceil(sqrt(self.nbasis))+1):
            t1_c = ceil(self.nbasis/i)
            for j in range(1, ceil(sqrt(self.nquad))+1):
                t1_r = ceil(self.nquad/j)
                for k in range(1, ceil(sqrt(self.nbasis))+1):
                    t2_r = ceil(self.nbasis/k)
                    for l in range(1, ceil(sqrt(self.nquad))+1):
                        t2_c = ceil(self.nquad/l)
                        if abs(t1_r*t1_c-t2_r*t2_c)/max(t1_r*t1_c, t2_c*t2_r) < 0.2:
                            tiles.append((t1_r, t1_c, t2_r, t2_c))

        # sort by least sync-ed config first
        tiles.sort(key=lambda T: self.get_local_barriers(*T))

        params = []

        for tile in tiles:
            for threads in threads_to_cells:
                best_cells = 10000
                for cells in threads_to_cells[threads]:
                    if (self.estimated_exec_time(TilingConfiguration(cells, threads,
                        *tile, False, False, True, True, False, False)) < self.estimated_exec_time(
                            TilingConfiguration(best_cells, threads, *tile,
                                False, False, True, True, False, False))):
                        best_cells = cells

                if best_cells != 10000:
                    params.append(TilingConfiguration(best_cells, threads, *tile,
                                False, False, True, True, False, False))

        # sort the parameters with highest occupancy.
        params.sort(key=lambda P:  self.estimated_exec_time(P))

        return params[:self.num_candidate_knls]

    @memoize_method
    def convert_numpy_arrays_to_cuda_mems(self, ary):
        ary = np.array(ary)
        ary_gpu = cuda.mem_alloc(ary.nbytes)
        cuda.memcpy_htod(src=ary, dest=ary_gpu)
        return ary_gpu

    def __call__(self, args, argshapes):

        best_performing_time = float("inf")
        best_performing_config = None
        nrounds = 15
        nwarmup = 5

        copied_args = args[:2]
        for i, arg in enumerate(self.fem_program.args[2:]):
            if arg.name in self.fem_program.root_kernel.get_written_variables():
                # arg is written during kernel execution => make a copy
                arg_gpu = cuda.mem_alloc(
                        int(np.prod(argshapes[i])*arg.dtype.itemsize))
                cuda.memcpy_dtod(src=args[i+2], dest=arg_gpu,
                        size=int(np.prod(argshapes[i])*arg.dtype.itemsize))
                copied_args += (arg_gpu,)
            else:
                # arg is read only => pass the same arg to the knl
                copied_args += (args[i+2],)

        from pyop2.gpu.tile import tiled_transform

        for tiling_config in self.get_candiate_configs():
            kernel, extra_args = tiled_transform(
                    self.fem_program.root_kernel, self.fem_program.callables_table,
                    tiling_config)
            from pymbolic import evaluate
            kernel = self.fem_program.with_root_kernel(kernel)
            code = lp.generate_code_v2(kernel).device_code()
            glens, llens = kernel.get_grid_size_upper_bounds_as_exprs()
            grid = tuple(int(evaluate(glens[i], {"start": args[0], "end":
                args[1]})) if i < len(glens) else 1
                    for i in range(2))
            block = tuple(int(evaluate(llens[i], {"start": args[0], "end":
                args[1]})) if i < len(llens) else 1
                    for i in range(3))
            executable_knl = SourceModule(code).get_function(kernel.name)
            executable_knl.prepare("i"*2+"P"*len(args[2:])+"P"*len(extra_args))
            extra_args = tuple(self.convert_numpy_arrays_to_cuda_mems(tuple(arg)) for arg
                    in extra_args)
            runtimes = []

            for i in range(nrounds):
                start_evt = cuda.Event()
                end_evt = cuda.Event()
                start_evt.record()
                start_evt.synchronize()
                executable_knl.prepared_call(grid, block, *(copied_args+extra_args))
                end_evt.record()
                end_evt.synchronize()
                runtimes.append(start_evt.time_till(end_evt)/1000)

            exec_time = np.mean(runtimes[nwarmup:])

            print("Params: {}, time={}".format(
                tiling_config, exec_time))

            if exec_time < best_performing_time:
                best_performing_time = exec_time
                best_performing_config = tiling_config

        return tiled_transform(
                self.fem_program.root_kernel, self.fem_program.callables_table,
                best_performing_config)

# }}}

# vim: fdm=marker