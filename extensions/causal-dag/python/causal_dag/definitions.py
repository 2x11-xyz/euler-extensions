"""Embedded fallback mirror of the shared annotation codebook.

The single source of truth for the human-facing definitions is the annotation
tool's ``definitions.json`` (2x11-xyz/causal-dag-annotation-tool). This module
mirrors it **verbatim** (codebook version 3) so a self-contained viewer render
carries the one codebook even without the private checkout - keep the two in
sync. ``viewer.load_definitions(path)`` reads an explicit codebook file;
without a path, this copy is the single, environment-independent default.

Since codebook v3 the definitions carry both the annotation tool's display
vocabulary and the v5-native kinds and statuses (``question``,
``investigation``, ``refuted``, ...), so viewer cards resolve every v5 key
directly - no alias bridging.
"""

from __future__ import annotations

# Mirrors causal-dag-annotation-tool/definitions.json (version 3), verbatim.
DEFINITIONS = {'edge_kinds': {'annotation': {'artifact_use': 'Its product feeds the target (arrow '
                                               'points in the direction of influence: '
                                               'producer to consumer).',
                               'evidence': 'Supports the target.',
                               'pivot': 'A failed branch inspired this one (not '
                                        'parentage).',
                               'refutation': 'Contradicts the target.',
                               'related': 'Loose association (symmetric; direction '
                                          'carries no meaning).',
                               'supersedes': 'Replaces the target.'},
                'structural': {'continuation': 'The same line of work continues.',
                               'decomposition': 'The parent split into subproblems.',
                               'fork': 'A branch point: sibling alternatives from one '
                                       'parent.',
                               'integration': 'Combines prior work into a joint '
                                              'result.',
                               'refinement': 'A narrower or sharper version of the '
                                             'parent.',
                               'repair': 'Fixes a failed parent; cites the failure it '
                                         'repairs.',
                               'verification': 'The act of checking a result.'}},
 'meta': {'direction': 'Every directed edge points in the direction of influence: the '
                       'from node acts on, feeds, or changes the standing of the to '
                       'node.',
          'version': 3},
 'node_kinds': {'attempt': 'A concrete try: an approach, implementation, or experiment '
                           'that can succeed or fail.',
                'checkpoint': "A consolidated state worth returning to: what's "
                              'established at this point.',
                'claim': 'An assertion about the world that evidence can support or '
                         'refute.',
                'investigation': 'A bounded effort undertaken to answer a question or '
                                 'assess a claim: an experiment, implementation, '
                                 'derivation, or study.',
                'question': 'A goal or question the work serves; questions decompose '
                            'into sub-questions.',
                'root': 'The goal or question anchoring a tree; everything else hangs '
                        'off it.',
                'synthesis': 'A node that pulls multiple branches together into a '
                             'combined result.'},
 'statuses': {'abandoned': 'Dropped without a verdict (priorities shifted); not proven '
                           'wrong.',
              'answered': 'The question has been answered; see the work beneath it.',
              'blocked': 'Cannot proceed until something external changes.',
              'dead_end': 'Tried and failed; do not revisit. (The most valuable signal '
                          'for a future agent.)',
              'inconclusive': "Investigated, but the evidence didn't settle it either "
                              'way.',
              'open': 'Still live; work or judgment pending.',
              'proven': 'Deductively established - reserved for domains where proof '
                        'exists.',
              'refuted': 'Shown false - decisive negative knowledge, the productive '
                         'end of a claim.',
              'stated': 'The synthesis is stated; its verification stands apart.',
              'succeeded': "It worked, but hasn't been independently confirmed.",
              'success': "It worked, but hasn't been independently confirmed.",
              'superseded': 'Replaced by something better; historically important, no '
                            'longer current.',
              'supported': 'Evidence weighs in favor; not conclusively established.',
              'verified': 'It worked and a check/test/independent evidence confirmed '
                          'it.'}}
