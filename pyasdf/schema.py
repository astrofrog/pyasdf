# Licensed under a 3-clause BSD style license - see LICENSE.rst
# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, unicode_literals, print_function

import json
import os

from astropy.extern import six
from astropy.utils.compat.odict import OrderedDict

from jsonschema import validators
from jsonschema.exceptions import ValidationError, SchemaError
import yaml

from .compat import lru_cache
from . import constants
from . import generic_io
from . import reference
from . import resolver as mresolver
from . import tagged
from . import util


if getattr(yaml, '__with_libyaml__', None):
    _yaml_base_loader = yaml.CSafeLoader
else:
    _yaml_base_loader = yaml.SafeLoader


__all__ = ['validate', 'fill_defaults', 'remove_defaults', 'check_schema']


SCHEMA_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), 'schemas'))


PYTHON_TYPE_TO_YAML_TAG = {
    None: 'null',
    six.text_type: 'str',
    bytes: 'str',
    bool: 'bool',
    int: 'int',
    float: 'float',
    list: 'seq',
    dict: 'map',
    set: 'set',
    OrderedDict: 'omap'
}


if six.PY2:
    PYTHON_TYPE_TO_YAML_TAG[long] = 'int'


# Prepend full YAML tag prefix
for k, v in PYTHON_TYPE_TO_YAML_TAG.items():
    PYTHON_TYPE_TO_YAML_TAG[k] = constants.YAML_TAG_PREFIX + v


def _type_to_tag(type_):
    for base in type_.mro():
        if base in PYTHON_TYPE_TO_YAML_TAG:
            return PYTHON_TYPE_TO_YAML_TAG[base]


def validate_tag(validator, tagname, instance, schema):
    # Shortcut: If the instance is a subclass of YAMLObject then we know it
    # should have a yaml_tag attribute attached; otherwise we have to use a
    # hack of reserializing the object and seeing what tags get attached to it
    # (though there may be a better way than this).

    # We can't validate tags at the top level of the schema, because
    # the tag information is lost.  This is, however, enforced by how
    # the rest of the system works, so it shouldn't matter.
    if schema == validator.schema:
        return

    if isinstance(tagname, bytes):
        tagname = tagname.decode('ascii')

    if isinstance(instance, tagged.Tagged):
        instance_tag = instance.tag
    else:
        # Try tags for known Python builtins
        instance_tag = _type_to_tag(type(instance))

    if instance_tag is not None and instance_tag != tagname:
        yield ValidationError(
            "mismatched tags, wanted '{0}', got '{1}'".format(
                tagname, instance_tag))


def validate_propertyOrder(validator, order, instance, schema):
    """
    Stores a value on the `tagged.TaggedDict` instance so that
    properties can be written out in the preferred order.  In that
    sense this isn't really a "validator", but using the `jsonschema`
    library's extensible validation system is the easiest way to get
    this property assigned.
    """
    if not validator.is_type(instance, 'object'):
        return

    if not order:
        # propertyOrder may be an empty list
        return

    instance.property_order = order


def validate_flowStyle(validator, flow_style, instance, schema):
    """
    Sets a flag on the `tagged.TaggedList` or `tagged.TaggedDict`
    object so that the YAML generator knows which style to use to
    write the element.  In that sense this isn't really a "validator",
    but using the `jsonschema` library's extensible validation system
    is the easiest way to get this property assigned.
    """
    if not (validator.is_type(instance, 'object') or
            validator.is_type(instance, 'array')):
        return

    instance.flow_style = flow_style


def validate_style(validator, style, instance, schema):
    """
    Sets a flag on the `tagged.TaggedString` object so that the YAML
    generator knows which style to use to write the string.  In that
    sense this isn't really a "validator", but using the `jsonschema`
    library's extensible validation system is the easiest way to get
    this property assigned.
    """
    if not validator.is_type(instance, 'string'):
        return

    instance.style = style


YAML_VALIDATORS = util.HashableDict(
    validators.Draft4Validator.VALIDATORS.copy())
YAML_VALIDATORS.update({
    'tag': validate_tag,
    'propertyOrder': validate_propertyOrder,
    'flowStyle': validate_flowStyle,
    'style': validate_style
})


def validate_fill_default(validator, properties, instance, schema):
    if not validator.is_type(instance, 'object'):
        return

    for property, subschema in six.iteritems(properties):
        if "default" in subschema:
            instance.setdefault(property, subschema["default"])


FILL_DEFAULTS = util.HashableDict({
    'properties': validate_fill_default,
})


def validate_remove_default(validator, properties, instance, schema):
    if not validator.is_type(instance, 'object'):
        return

    for property, subschema in six.iteritems(properties):
        if "default" in subschema:
            if instance.get(property, None) == subschema["default"]:
                del instance[property]


REMOVE_DEFAULTS = util.HashableDict({
    'properties': validate_remove_default
})


@lru_cache()
def _create_validator(_validators=YAML_VALIDATORS):
    validator = validators.create(
        meta_schema=load_schema(
            'http://stsci.edu/schemas/yaml-schema/draft-01',
            mresolver.default_url_mapping),
        validators=_validators)
    validator.orig_iter_errors = validator.iter_errors

    # We can't validate anything that looks like an external
    # reference, since we don't have the actual content, so we
    # just have to defer it for now.  If the user cares about
    # complete validation, they can call
    # `AsdfFile.resolve_references`.
    def iter_errors(self, instance, _schema=None, _seen=set()):
        if id(instance) in _seen:
            return

        if _schema is None:
            schema = self.schema
        else:
            schema = _schema

        if ((isinstance(instance, dict) and '$ref' in instance) or
            isinstance(instance, reference.Reference)):
            return

        if _schema is None:
            tag = tagged.get_tag(instance)
            if tag is not None:
                schema_path = self.ctx.tag_to_schema_resolver(tag)
                if schema_path != tag:
                    s = load_schema(schema_path, self.ctx.url_mapping)
                    if s:
                        with self.resolver.in_scope(schema_path):
                            for x in self.orig_iter_errors(instance, s):
                                yield x

            if isinstance(instance, dict):
                new_seen = _seen | set([id(instance)])
                for val in six.itervalues(instance):
                    for x in self.iter_errors(val, _seen=new_seen):
                        yield x

            elif isinstance(instance, list):
                new_seen = _seen | set([id(instance)])
                for val in instance:
                    for x in self.iter_errors(val, _seen=new_seen):
                        yield x
        else:
            for x in self.orig_iter_errors(instance, _schema=schema):
                yield x

    validator.iter_errors = iter_errors
    return validator


@lru_cache()
def _load_schema(url):
    with generic_io.get_file(url) as fd:
        if isinstance(url, six.text_type) and url.endswith('json'):
            result = json.load(fd)
        else:
            result = yaml.load(fd, Loader=_yaml_base_loader)
    return result


def _make_schema_loader(resolver):
    def load_schema(url):
        url = resolver(url)
        return _load_schema(url)
    return load_schema


def _make_resolver(url_mapping):
    handlers = {}
    schema_loader = _make_schema_loader(url_mapping)
    for x in ['http', 'https', 'file']:
        handlers[x] = schema_loader

    # We set cache_remote=False here because we do the caching of
    # remote schemas here in `load_schema`, so we don't need
    # jsonschema to do it on our behalf.  Setting it to `True`
    # counterintuitively makes things slower.
    return validators.RefResolver(
        '', {}, cache_remote=False, handlers=handlers)


def load_schema(url, resolver=None):
    """
    Load a schema from the given URL.

    Parameters
    ----------
    url : str
        The path to the schema

    resolver : callable, optional
        A callback function used to map URIs to other URIs.  The
        callable must take a string and return a string or `None`.
        This is useful, for example, when a remote resource has a
        mirror on the local filesystem that you wish to use.
    """
    if resolver is None:
        resolver = mresolver.default_url_mapping
    loader = _make_schema_loader(resolver)
    return loader(url)


def validate(instance, ctx, _validators=YAML_VALIDATORS, *args, **kwargs):
    """
    Validate the given instance (which must be a tagged tree) against
    the appropriate schema.  The schema itself is located using the
    tag on the instance.

    The additional *args and **kwargs are passed along to
    `jsonschema.validate`.

    Parameters
    ----------
    instance : tagged tree

    ctx : AsdfFile context
        Used to resolve tags and urls
    """
    kwargs['resolver'] = _make_resolver(ctx.url_mapping)

    # We don't just call validators.validate() directly here, because
    # that validates the schema itself, wasting a lot of time (at the
    # time of this writing, it was half of the runtime of the unit
    # test suite!!!).  Instead, we assume that the schemas are valid
    # through the running of the unit tests, not at run time.
    cls = _create_validator(_validators=_validators)
    validator = cls({}, *args, **kwargs)
    validator.ctx = ctx
    validator.validate(instance)


def fill_defaults(instance, ctx):
    """
    For any default values in the schema, add them to the tree if they
    don't exist.

    Parameters
    ----------
    instance : tagged tree

    ctx : AsdfFile context
        Used to resolve tags and urls
    """
    validate(instance, ctx, FILL_DEFAULTS)


def remove_defaults(instance, ctx):
    """
    For any values in the tree that are the same as the default values
    specified in the schema, remove them from the tree.

    Parameters
    ----------
    instance : tagged tree

    ctx : AsdfFile context
        Used to resolve tags and urls
    """
    validate(instance, ctx, REMOVE_DEFAULTS)


def check_schema(schema):
    """
    Check a given schema to make sure it is valid YAML schema.
    """
    # We also want to validate the "default" values in the schema
    # against the schema itself.  jsonschema as a library doesn't do
    # this on its own.

    def validate_default(validator, default, instance, schema):
        if not validator.is_type(instance, 'object'):
            return

        if 'default' in instance:
            with instance_validator.resolver.in_scope(scope):
                for err in instance_validator.iter_errors(
                        instance['default'], instance):
                    yield err

    VALIDATORS = util.HashableDict(
        validators.Draft4Validator.VALIDATORS.copy())
    VALIDATORS.update({
        'default': validate_default
    })

    meta_schema = load_schema(
        'http://stsci.edu/schemas/yaml-schema/draft-01',
        mresolver.default_url_mapping)

    resolver = _make_resolver(mresolver.default_url_mapping)

    cls = validators.create(meta_schema=meta_schema,
                            validators=VALIDATORS)
    validator = cls(meta_schema, resolver=resolver)

    instance_validator = validators.Draft4Validator(schema, resolver=resolver)
    scope = schema.get('id', '')

    validator.validate(schema, _schema=meta_schema)
