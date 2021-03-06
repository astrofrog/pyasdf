# Licensed under a 3-clause BSD style license - see LICENSE.rst
# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, unicode_literals, print_function

import atexit
import io
import os
import shutil
import tempfile
import textwrap
import codecs

from docutils.parsers.rst import Directive
from docutils import nodes

from sphinx.util.nodes import set_source_info

from pyasdf import AsdfFile
from pyasdf.constants import ASDF_MAGIC, BLOCK_FLAG_STREAMED
from pyasdf import versioning
from pyasdf import yamlutil

version_string = versioning.version_to_string(versioning.default_version)


TMPDIR = tempfile.mkdtemp()

def delete_tmpdir():
    shutil.rmtree(TMPDIR)


GLOBALS = {}
LOCALS = {}


FLAGS = {
    BLOCK_FLAG_STREAMED: "BLOCK_FLAG_STREAMED"
}


class RunCodeDirective(Directive):
    has_content = True

    def run(self):
        code = textwrap.dedent('\n'.join(self.content))

        cwd = os.getcwd()
        os.chdir(TMPDIR)

        try:
            try:
                exec(code, GLOBALS, LOCALS)
            except:
                print(code)
                raise

            literal = nodes.literal_block(code, code)
            literal['language'] = 'python'
            set_source_info(self, literal)
        finally:
            os.chdir(cwd)
        return [literal]


class AsdfDirective(Directive):
    required_arguments = 1

    def run(self):
        filename = self.arguments[0]

        cwd = os.getcwd()
        os.chdir(TMPDIR)

        parts = []
        try:
            code = AsdfFile.read(filename, _get_yaml_content=True)
            code = '{0}{1}\n'.format(ASDF_MAGIC, version_string) + code.strip().decode('utf-8')
            literal = nodes.literal_block(code, code)
            literal['language'] = 'yaml'
            set_source_info(self, literal)
            parts.append(literal)

            ff = AsdfFile.read(filename)
            for i, block in enumerate(ff.blocks.internal_blocks):
                data = codecs.encode(block.data.tostring(), 'hex')
                if len(data) > 40:
                    data = data[:40] + '...'.encode()
                allocated = block._allocated
                size = block._size
                data_size = block._data_size
                flags = block._flags

                if flags & BLOCK_FLAG_STREAMED:
                    allocated = size = data_size = 0

                lines = []
                lines.append('BLOCK {0}:'.format(i))

                human_flags = []
                for key, val in FLAGS.items():
                    if flags & key:
                        human_flags.append(val)
                if len(human_flags):
                    lines.append('    flags: {0}'.format(' | '.join(human_flags)))
                if block.compression:
                    lines.append('    compression: {0}'.format(block.compression))
                lines.append('    allocated_size: {0}'.format(allocated))
                lines.append('    used_size: {0}'.format(size))
                lines.append('    data_size: {0}'.format(data_size))
                lines.append('    data: {0}'.format(data))

                code = '\n'.join(lines)

                literal = nodes.literal_block(code, code)
                literal['language'] = 'yaml'
                set_source_info(self, literal)
                parts.append(literal)

        finally:
            os.chdir(cwd)

        result = nodes.admonition()
        textnodes, messages = self.state.inline_text(filename, self.lineno)
        title = nodes.title(filename, '', *textnodes)
        result += title
        result.children.extend(parts)
        return [result]


def setup(app):
    app.add_directive('runcode', RunCodeDirective)
    app.add_directive('asdf', AsdfDirective)
    atexit.register(delete_tmpdir)
