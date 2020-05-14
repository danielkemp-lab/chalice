import io
import os
import zipfile
import json
import contextlib
import tempfile
import re
import shutil
import sys
import tarfile
import subprocess


import click
from collections import OrderedDict # noqa
from typing import IO, Dict, List, Any, Tuple, Iterator, BinaryIO, Text  # noqa
from typing import Optional, Union  # noqa
from typing import MutableMapping  # noqa
from typing import cast  # noqa

from botocore.loaders import create_loader
from botocore.regions import EndpointResolver

from chalice.constants import WELCOME_PROMPT

OptInt = Optional[int]
OptStr = Optional[str]
EnvVars = MutableMapping


class AbortedError(Exception):
    pass


def to_cfn_resource_name(name):
    # type: (str) -> str
    """Transform a name to a valid cfn name.

    This will convert the provided name to a CamelCase name.
    It's possible that the conversion to a CFN resource name
    can result in name collisions.  It's up to the caller
    to handle name collisions appropriately.

    """
    if not name:
        raise ValueError("Invalid name: %r" % name)
    word_separators = ['-', '_']
    for word_separator in word_separators:
        word_parts = [p for p in name.split(word_separator) if p]
        name = ''.join([w[0].upper() + w[1:] for w in word_parts])
    return re.sub(r'[^A-Za-z0-9]+', '', name)


def remove_stage_from_deployed_values(key, filename):
    # type: (str, str) -> None
    """Delete a top level key from the deployed JSON file."""
    final_values = {}  # type: Dict[str, Any]
    try:
        with open(filename, 'r') as f:
            final_values = json.load(f)
    except IOError:
        # If there is no file to delete from, then this funciton is a noop.
        return

    try:
        del final_values[key]
        with open(filename, 'wb') as f:
            data = serialize_to_json(final_values)
            f.write(data.encode('utf-8'))
    except KeyError:
        # If they key didn't exist then there is nothing to remove.
        pass


def record_deployed_values(deployed_values, filename):
    # type: (Dict[str, Any], str) -> None
    """Record deployed values to a JSON file.

    This allows subsequent deploys to lookup previously deployed values.

    """
    final_values = {}  # type: Dict[str, Any]
    if os.path.isfile(filename):
        with open(filename, 'r') as f:
            final_values = json.load(f)
    final_values.update(deployed_values)
    with open(filename, 'wb') as f:
        data = serialize_to_json(final_values)
        f.write(data.encode('utf-8'))


def serialize_to_json(data):
    # type: (Any) -> str
    """Serialize to pretty printed JSON.

    This includes using 2 space indentation, no trailing whitespace, and
    including a newline at the end of the JSON document.  Useful when you want
    to serialize JSON to disk.

    """
    return json.dumps(data, indent=2, separators=(',', ': ')) + '\n'


def create_resolver():
    # type: () -> EndpointResolver
    """Establish an EndpointResolver via botocore standards

    This allows the dns suffix for the different regions and partitions to be
    discovered throughout the chalice microframework

    """
    loader = create_loader('data_loader')
    endpoints = loader.load_data('endpoints')
    return EndpointResolver(endpoints)


def resolve_endpoint(service, region):
    # type: (str, str) -> Union[OrderedDict[str, Any], None]
    """Find details of an endpoint based on the service and region

    This utilizes the botocore EndpointResolver in order to find details on
    the given service and region combination.  If the service and region
    combination is not found the None will be returned.

    """
    return create_resolver().construct_endpoint(service, region)


def endpoint_from_arn(arn):
    # type: (str) -> Union[OrderedDict[str, Any], None]
    """Find details for the endpoint associated with a resource ARN

    This allows the an endpoint to be discerned based on an ARN.  This
    is a convenience method due to the need to parse multiple ARNs
    throughout the project. If the service and region combination
    is not found the None will be returned.

    """
    arn_split = arn.split(':')
    return resolve_endpoint(arn_split[2], arn_split[3])


def endpoint_dns_suffix(service, region):
    # type: (str, str) -> str
    """Discover the dns suffix for a given service and region combination

    This allows the service DNS suffix to be discoverable throughout the
    framework.  If the ARN's service and region combination is not found
    then amazonaws.com is returned.

    """
    endpoint = resolve_endpoint(service, region)
    return endpoint['dnsSuffix'] if endpoint else 'amazonaws.com'


def endpoint_dns_suffix_from_arn(arn):
    # type: (str) -> str
    """Discover the dns suffix for a given ARN

    This allows the service DNS suffix to be discoverable throughout the
    framework based on the ARN.  If the ARN's service and region
    combination is not found then amazonaws.com is returned.

    """
    endpoint = endpoint_from_arn(arn)
    return endpoint['dnsSuffix'] if endpoint else 'amazonaws.com'


class ChaliceZipFile(zipfile.ZipFile):
    """Support deterministic zipfile generation.

    Normalizes datetime and permissions.

    """

    compression = 0  # Try to make mypy happy.
    _default_time_time = (1980, 1, 1, 0, 0, 0)

    def __init__(self, *args, **kwargs):
        # type: (Any, Any) -> None
        self._osutils = cast(OSUtils, kwargs.pop('osutils', OSUtils()))
        super(ChaliceZipFile, self).__init__(*args, **kwargs)

    # pylint: disable=W0221
    def write(self, filename, arcname=None, compress_type=None):
        # type: (Text, Optional[Text], Optional[int]) -> None
        # Only supports files, py2.7 and 3 have different signatures.
        # We know that in our packager code we never call write() on
        # directories.
        zinfo = self._create_zipinfo(filename, arcname, compress_type)
        with open(filename, 'rb') as f:
            self.writestr(zinfo, f.read())

    def _create_zipinfo(self, filename, arcname, compress_type):
        # type: (Text, Optional[Text], Optional[int]) -> zipfile.ZipInfo
        # The main thing that prevents deterministic zip file generation
        # is that the mtime of the file is included in the zip metadata.
        # We don't actually care what the mtime is when we run on lambda,
        # so we always set it to the default value (which comes from
        # zipfile.py).  This ensures that as long as the file contents don't
        # change (or the permissions) then we'll always generate the exact
        # same zip file bytes.
        # We also can't use ZipInfo.from_file(), it's only in python3.
        st = self._osutils.stat(str(filename))
        if arcname is None:
            arcname = filename
        arcname = self._osutils.normalized_filename(str(arcname))
        arcname = arcname.lstrip(os.sep)
        zinfo = zipfile.ZipInfo(arcname, self._default_time_time)
        # The external_attr needs the upper 16 bits to be the file mode
        # so we have to shift it up to the right place.
        zinfo.external_attr = (st.st_mode & 0xFFFF) << 16
        zinfo.file_size = st.st_size
        zinfo.compress_type = compress_type or self.compression
        return zinfo


def create_zip_file(source_dir, outfile):
    # type: (str, str) -> None
    """Create a zip file from a source input directory.

    This function is intended to be an equivalent to
    `zip -r`.  You give it a source directory, `source_dir`,
    and it will recursively zip up the files into a zipfile
    specified by the `outfile` argument.

    """
    with ChaliceZipFile(outfile, 'w',
                        compression=zipfile.ZIP_DEFLATED,
                        osutils=OSUtils()) as z:
        for root, _, filenames in os.walk(source_dir):
            for filename in filenames:
                full_name = os.path.join(root, filename)
                archive_name = os.path.relpath(full_name, source_dir)
                z.write(full_name, archive_name)


class OSUtils(object):
    ZIP_DEFLATED = zipfile.ZIP_DEFLATED

    def environ(self):
        # type: () -> MutableMapping
        return os.environ

    def open(self, filename, mode):
        # type: (str, str) -> IO
        return open(filename, mode)

    def open_zip(self, filename, mode, compression=ZIP_DEFLATED):
        # type: (str, str, int) -> zipfile.ZipFile
        return ChaliceZipFile(filename, mode, compression=compression,
                              osutils=self)

    def remove_file(self, filename):
        # type: (str) -> None
        """Remove a file, noop if file does not exist."""
        # Unlike os.remove, if the file does not exist,
        # then this method does nothing.
        try:
            os.remove(filename)
        except OSError:
            pass

    def file_exists(self, filename):
        # type: (str) -> bool
        return os.path.isfile(filename)

    def get_file_contents(self, filename, binary=True, encoding='utf-8'):
        # type: (str, bool, Any) -> str
        # It looks like the type definition for io.open is wrong.
        # the encoding arg is unicode, but the actual type is
        # Optional[Text].  For now we have to use Any to keep mypy happy.
        if binary:
            mode = 'rb'
            # In binary mode the encoding is not used and most be None.
            encoding = None
        else:
            mode = 'r'
        with io.open(filename, mode, encoding=encoding) as f:
            return f.read()

    def set_file_contents(self, filename, contents, binary=True):
        # type: (str, str, bool) -> None
        if binary:
            mode = 'wb'
        else:
            mode = 'w'
        with open(filename, mode) as f:
            f.write(contents)

    def extract_zipfile(self, zipfile_path, unpack_dir):
        # type: (str, str) -> None
        with zipfile.ZipFile(zipfile_path, 'r') as z:
            z.extractall(unpack_dir)

    def extract_tarfile(self, tarfile_path, unpack_dir):
        # type: (str, str) -> None
        with tarfile.open(tarfile_path, 'r:*') as tar:
            tar.extractall(unpack_dir)

    def directory_exists(self, path):
        # type: (str) -> bool
        return os.path.isdir(path)

    def get_directory_contents(self, path):
        # type: (str) -> List[str]
        return os.listdir(path)

    def makedirs(self, path):
        # type: (str) -> None
        os.makedirs(path)

    def dirname(self, path):
        # type: (str) -> str
        return os.path.dirname(path)

    def abspath(self, path):
        # type: (str) -> str
        return os.path.abspath(path)

    def joinpath(self, *args):
        # type: (str) -> str
        return os.path.join(*args)

    def walk(self, path):
        # type: (str) -> Iterator[Tuple[str, List[str], List[str]]]
        return os.walk(path)

    def copytree(self, source, destination):
        # type: (str, str) -> None
        if not os.path.exists(destination):
            self.makedirs(destination)
        names = self.get_directory_contents(source)
        for name in names:
            new_source = os.path.join(source, name)
            new_destination = os.path.join(destination, name)
            if os.path.isdir(new_source):
                self.copytree(new_source, new_destination)
            else:
                shutil.copy2(new_source, new_destination)

    def rmtree(self, directory):
        # type: (str) -> None
        shutil.rmtree(directory)

    def copy(self, source, destination):
        # type: (str, str) -> None
        shutil.copy(source, destination)

    def move(self, source, destination):
        # type: (str, str) -> None
        shutil.move(source, destination)

    @contextlib.contextmanager
    def tempdir(self):
        # type: () -> Any
        tempdir = tempfile.mkdtemp()
        try:
            yield tempdir
        finally:
            shutil.rmtree(tempdir)

    def popen(self, command, stdout=None, stderr=None, env=None):
        # type: (List[str], OptInt, OptInt, EnvVars) -> subprocess.Popen
        p = subprocess.Popen(command, stdout=stdout, stderr=stderr, env=env)
        return p

    def mtime(self, path):
        # type: (str) -> int
        return os.stat(path).st_mtime

    def stat(self, path):
        # type: (str)  -> os.stat_result
        return os.stat(path)

    def normalized_filename(self, path):
        # type: (str) -> str
        """Normalize a path into a filename.

        This will normalize a file and remove any 'drive' component
        from the path on OSes that support drive specifications.

        """
        return os.path.normpath(os.path.splitdrive(path)[1])

    @property
    def pipe(self):
        # type: () -> int
        return subprocess.PIPE


def getting_started_prompt(prompter):
    # type: (Any) -> bool
    return prompter.prompt(WELCOME_PROMPT)


class UI(object):
    def __init__(self, out=None, err=None, confirm=None):
        # type: (Optional[IO], Optional[IO], Any) -> None
        # I tried using a more exact type for the 'confirm'
        # param, but mypy seems to miss the 'if confirm is None'
        # check and types _confirm as Union[..., None].
        # So for now, we're using Any for this type.
        if out is None:
            out = sys.stdout
        if err is None:
            err = sys.stderr
        if confirm is None:
            confirm = click.confirm
        self._out = out
        self._err = err
        self._confirm = confirm

    def write(self, msg):
        # type: (str) -> None
        self._out.write(msg)

    def error(self, msg):
        # type: (str) -> None
        self._err.write(msg)

    def confirm(self, msg, default=False, abort=False):
        # type: (str, bool, bool) -> Any
        try:
            return self._confirm(msg, default, abort)
        except click.Abort:
            raise AbortedError()


class PipeReader(object):
    def __init__(self, stream):
        # type: (IO[str]) -> None
        self._stream = stream

    def read(self):
        # type: () -> OptStr
        if not self._stream.isatty():
            return self._stream.read()
        return None
