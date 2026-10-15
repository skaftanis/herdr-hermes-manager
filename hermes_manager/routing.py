"""Decode profile-qualified restore references without another local integration."""
import re
from .core import explicit_profile, get_conversation


def resume_arguments(argv, settings):
    result = argv[:]
    for index, arg in enumerate(argv):
        if arg == '--': break
        inline = arg.startswith('--resume=')
        if arg != '--resume' and not inline: continue
        position = index if inline else index+1
        if position >= len(argv): break
        value = arg.partition('=')[2] if inline else argv[position]
        if not value.startswith('herdr-profile:'): continue
        name, separator, native = value[len('herdr-profile:'):].partition(':')
        if not separator or not native:
            raise ValueError('Invalid profile-qualified resume reference')
        profile = settings.profile(name)
        selected = explicit_profile(argv)
        if selected is not None and selected != name:
            raise ValueError('Saved conversation belongs to another profile')
        if re.fullmatch(r'fresh_[0-9a-f]{32}', native):
            del result[index:position+1]
        else:
            get_conversation(profile, native)
            result[position] = '--resume='+native if inline else native
        return result if selected is not None else ['-p', name, *result]
    return result
