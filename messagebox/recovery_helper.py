"""Verified recovery helper protocol 1; no controller exchange in PR 3.

The permanent launcher supplies bounded, validated state. This helper has no
imports from the application, controller, or neighboring helper files.
"""


def recover(context):
    if (context.get('journal') not in ({'schema': 1, 'kind': 'bootstrap', 'phase': phase}
                                             for phase in ('prepared', 'armed'))):
        raise ValueError('unsupported_recovery_state')
    if context['check_legacy']() is not True:
        raise ValueError('legacy_controller_invalid')
    return 'legacy'
