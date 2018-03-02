#!/usr/bin/env python
# -*- coding: utf-8 -*-
# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
"""
.. _sdc_base :

Automatic selection of the appropriate SDC method
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If the dataset metadata indicate tha more than one field map acquisition is
``IntendedFor`` (see BIDS Specification section 8.9) the following priority will
be used:

  1. :ref:`sdc_pepolar` (or **blip-up/blip-down**)

  2. :ref:`sdc_direct_b0`

  3. :ref:`sdc_phasediff`

  4. :ref:`sdc_fieldmapless`


Table of behavior (fieldmap use-cases):

=============== =========== ============= ===============
Fieldmaps found ``use_syn`` ``force_syn``     Action
=============== =========== ============= ===============
True            *           True          Fieldmaps + SyN
True            *           False         Fieldmaps
False           *           True          SyN
False           True        False         SyN
False           False       False         HMC only
=============== =========== ============= ===============


"""

from niworkflows.nipype.pipeline import engine as pe
from niworkflows.nipype.interfaces import utility as niu

# Fieldmap workflows
from . import (
    init_pepolar_unwarp_wf,
    init_syn_sdc_wf,
    init_fmap_unwarp_report_wf
)

from niworkflows.nipype import logging
LOGGER = logging.getLogger('workflow')
FMAP_PRIORITY = {
    'epi': 0,
    'fieldmap': 1,
    'phasediff': 2,
    'syn': 3
}


def init_sdc_wf(layout, fmaps, template=None, bold_file=None, omp_nthreads=1):
    """
    This workflow implements the heuristics to choose a
    :abbr:`susceptibility distortion correction (SDC)` strategy.
    When no field map information is present in the BIDS inputs,
    the EXPERIMENTAL "fieldmap-less SyN" can be performed, using
    the ``--use-syn`` and ``--force-syn`` flags.


    """

    # TODO: To be removed (supported fieldmaps):
    if not set([fmap['type'] for fmap in fmaps]).intersection(FMAP_PRIORITY):
        fmaps = None

    workflow = pe.Workflow(name='sdc_wf' if fmaps else 'sdc_bypass_wf')
    inputnode = pe.Node(niu.IdentityInterface(
        fields=['name_source', 'bold_ref', 'bold_ref_brain', 'bold_mask',
                'fmap', 'fmap_ref', 'fmap_mask', 't1_brain', 't1_seg',
                't1_2_mni_reverse_transform', 'itk_t1_to_bold']),
        name='inputnode')

    outputnode = pe.Node(niu.IdentityInterface(
        fields=['bold_ref', 'bold_mask', 'bold_ref_brain',
                'out_warp', 'out_report', 'syn_sdc_report']),
        name='outputnode')

    # No fieldmaps - forward inputs to outputs
    if not fmaps:
        workflow.connect([
            (inputnode, outputnode, [('bold_ref', 'bold_ref'),
                                     ('bold_mask', 'bold_mask'),
                                     ('bold_ref_brain', 'bold_ref_brain')]),
        ])
        return workflow

    bold_meta = layout.get_metadata(bold_file)

    # In case there are multiple fieldmaps prefer EPI
    fmaps.sort(key=lambda fmap: FMAP_PRIORITY[fmap['type']])
    fmap = fmaps[0]

    # PEPOLAR path
    if fmap['type'] == 'epi':
        setattr(workflow, 'sdc_method', 'PEB/PEPOLAR (phase-encoding based / PE-POLARity)')
        epi_fmaps = [fmap_['epi'] for fmap_ in fmaps if fmap_['type'] == 'epi']
        sdc_unwarp_wf = init_pepolar_unwarp_wf(
            bold_meta=bold_meta,
            epi_fmaps=[(epi, layout.get_metadata(epi)["PhaseEncodingDirection"])
                       for epi in epi_fmaps],
            omp_nthreads=omp_nthreads,
            name='pepolar_unwarp_wf')

    # FIELDMAP path
    if fmap['type'] in ['fieldmap', 'phasediff']:
        setattr(workflow, 'sdc_method', 'FMB (%s-based)' % fmap['type'])
        # Import specific workflows here, so we don't break everything with one
        # unused workflow.
        from ..fieldmap import init_sdc_unwarp_wf

        if fmap['type'] == 'fieldmap':
            from .fmap import init_fmap_wf
            fmap_estimator_wf = init_fmap_wf(
                reportlets_dir=reportlets_dir,
                omp_nthreads=omp_nthreads,
                fmap_bspline=fmap_bspline)
            # set inputs
            fmap_estimator_wf.inputs.inputnode.fieldmap = fmap['fieldmap']
            fmap_estimator_wf.inputs.inputnode.magnitude = fmap['magnitude']

        if fmap['type'] == 'phasediff':
            from .phdiff import init_phdiff_wf
            fmap_estimator_wf = init_phdiff_wf(
                reportlets_dir=reportlets_dir,
                omp_nthreads=omp_nthreads)
            # set inputs
            fmap_estimator_wf.inputs.inputnode.phasediff = fmap['phasediff']
            fmap_estimator_wf.inputs.inputnode.magnitude = [
                fmap_ for key, fmap_ in sorted(fmap.items())
                if key.startswith("magnitude")
            ]

        sdc_unwarp_wf = init_sdc_unwarp_wf(
            reportlets_dir=reportlets_dir,
            omp_nthreads=omp_nthreads,
            fmap_demean=fmap_demean,
            debug=debug,
            name='sdc_unwarp_wf')

        workflow.connect([
            (fmap_estimator_wf, sdc_unwarp_wf, [
                ('outputnode.fmap', 'inputnode.fmap'),
                ('outputnode.fmap_ref', 'inputnode.fmap_ref'),
                ('outputnode.fmap_mask', 'inputnode.fmap_mask')]),
        ])

    # FIELDMAP-less path
    if fmaps[-1]['type'] == 'syn':
        syn_sdc_wf = init_syn_sdc_wf(
            template=template,
            bold_pe=bold_meta.get('PhaseEncodingDirection', None),
            omp_nthreads=omp_nthreads)

        workflow.connect([
            (inputnode, syn_sdc_wf, [
                ('t1_brain', 'inputnode.t1_brain'),
                ('t1_seg', 'inputnode.t1_seg'),
                ('t1_2_mni_reverse_transform', 'inputnode.t1_2_mni_reverse_transform'),
                ('bold_ref_brain', 'inputnode.bold_ref')]),
        ])

        # XXX Eliminate branch when forcing isn't an option
        if len(fmaps) == 1:  # --force-syn was called
            setattr(workflow, 'sdc_method', 'FLB (fieldmap-less SyN)')
            sdc_unwarp_wf = syn_sdc_wf
        else:
            workflow.connect([
                (syn_sdc_wf, outputnode, [
                    ('outputnode.out_warp_report', 'syn_sdc_report')]),
            ])

    sdc_unwarp_wf.connect([
        (syn_sdc_wf, outputnode, [
            ('outputnode.out_warp', 'inputnode.out_warp'),
            ('outputnode.out_reference_brain', 'inputnode.ref_bold_brain'),
            ('outputnode.out_mask', 'inputnode.ref_bold_mask')]),
    ])

    # Report on BOLD correction
    fmap_unwarp_report_wf = init_fmap_unwarp_report_wf(
        reportlets_dir=reportlets_dir,
        name='fmap_unwarp_report_wf')
    workflow.connect([
        (inputnode, fmap_unwarp_report_wf, [
            ('t1_seg', 'inputnode.in_seg'),
            ('name_source', 'inputnode.name_source'),
            ('bold_ref', 'inputnode.in_pre'),
            ('itk_t1_to_bold', 'inputnode.in_xfm')]),
        (sdc_unwarp_wf, fmap_unwarp_report_wf, [
            ('outputnode.out_reference', 'inputnode.in_post')]),
    ])
