import re
import os
import json
import datetime
from hashlib import sha1
from tempfile import NamedTemporaryFile

from kolekto.printer import printer, option
from kolekto.commands import Command
from kolekto.commands.show import show
from kolekto.datasources import MovieDatasource
from kolekto.exceptions import KolektoRuntimeError
from kolekto.db import AttachmentStore


def clean_title(title):
    title = re.sub('[._-]', ' ', title).strip().title()
    match = re.match('(.+)((19|2\d)\d\d)', title)
    if match is None:
        return None, title
    else:
        title = match.group(1)
        year = int(match.group(2))
        return year, title


def clean_title_series(title):
    title = re.sub('[._-]', ' ', title).strip().title()
    match = re.match('(.+?) S(\d+)E(\d+)', title)
    if match is None:
        return title, None, None
    else:
        return match.group(1), int(match.group(2)), int(match.group(3))


def copy(tree, source_filename):
    """ Copy file in tree, show a progress bar during operations,
        and return the sha1 sum of copied file.
    """
    #_, ext = os.path.splitext(source_filename)
    filehash = sha1()
    with printer.progress(os.path.getsize(source_filename)) as update:
        with open(source_filename, 'rb') as fsource:
            with NamedTemporaryFile(dir=os.path.join(tree, '.kolekto', 'movies'), delete=False) as fdestination:
                # Copy the source into the temporary destination:
                while True:
                    buf = fsource.read(10 * 1024)
                    if not buf:
                        break
                    filehash.update(buf)
                    fdestination.write(buf)
                    update(len(buf))
                # Rename the file to its final name or raise an error if
                # the file already exists:
                dest = os.path.join(tree, '.kolekto', 'movies', filehash.hexdigest())
                if os.path.exists(dest):
                    raise IOError('This file already exists in tree (%s)' % filehash.hexdigest())
                else:
                    os.rename(fdestination.name, dest)
    return filehash.hexdigest()


def link(tree, source_filename, symlink=False):
    filehash = sha1()
    with printer.progress(os.path.getsize(source_filename)) as update:
        with open(source_filename, 'rb') as fsource:
                # Copy the source into the temporary destination:
                while True:
                    buf = fsource.read(10 * 1024)
                    if not buf:
                        break
                    filehash.update(buf)
                    update(len(buf))
                # Hardlink the file or raise an error if the file already exists:
                dest = os.path.join(tree, '.kolekto', 'movies', filehash.hexdigest())
                if os.path.exists(dest):
                    raise IOError('This file already exists in tree (%s)' % filehash.hexdigest())
                else:
                    if symlink:
                        source_filename = os.path.relpath(source_filename,
                                                          os.path.join(tree, '.kolekto', 'movies'))
                        os.symlink(source_filename, dest)
                    else:
                        os.link(source_filename, dest)
    return filehash.hexdigest()


def list_attachments(fullname):
    """ List attachment for the specified fullname.
    """
    parent, filename = os.path.split(fullname)
    filename_without_ext, ext = os.path.splitext(filename)
    attachments = []
    for found_filename in os.listdir(parent):
        found_filename_without_ext, _ = os.path.splitext(found_filename)
        if filename_without_ext == found_filename_without_ext and found_filename != filename:
            attachments.append(os.path.join(parent, found_filename))
    return attachments


class BaseImport(Command):

    """ Base class for importers.
    """

    def prepare(self):
        self.add_arg('file', nargs='+', help='Files to import (globbing allowed)')
        self.add_arg('--hardlink', action='store_true', default=False,
                     help='Create an hardlink instead of copying the file')
        self.add_arg('--symlink', action='store_true', default=False,
                     help='Create a symlink instead of copying the file')
        self.add_arg('--auto', '-a', action='store_true', default=False,
                     help='Automatically choose the most revelent movie.')
        self.add_arg('--dont-show', dest='show', action='store_false', default=True,
                     help='Show all informations about imported movie.')
        self.add_arg('--delete', action='store_true', default=False,
                     help='Delete imported file after a successful import')
        self.add_arg('--dont-import-attachments', dest='import_attachments',
                     action='store_false', default=True,
                     help='Don\'t import the attachments')

    def run(self, args, config):
        # Check the args:
        if args.symlink and args.delete:
            raise KolektoRuntimeError('--delete can\'t be used with --symlink')
        elif args.symlink and args.hardlink:
            raise KolektoRuntimeError('--symlink and --hardlink are mutually exclusive')

        # Load the metadata database:
        mdb = self.get_metadata_db(args.tree)

        # Load informations from db:
        mds = MovieDatasource(config.subsections('datasource'), args.tree, self.profile.object_class)

        attachment_store = AttachmentStore(os.path.join(args.tree, '.kolekto', 'attachments'))

        for filename in args.file:
            filename = filename.decode('utf8')
            movie = self._import(mdb, mds, args, config, filename)

            # Refresh the full data for the choosen movie:
            movie = mds.refresh(movie)

            # Append the import date
            movie['import_date'] = datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')

            if args.show:
                show(movie)
                printer.p('')

            # Edit available data:
            if not args.auto and printer.ask('Do you want to edit the movie metadata', default=False):
                movie = self.profile.object_class(json.loads(printer.edit(json.dumps(movie, indent=True))))

            # Hardlink or copy the movie in the tree
            if args.hardlink or args.symlink:
                printer.p('\nComputing movie sha1sum...')
                movie_hash = link(args.tree, filename, args.symlink)
            else:
                printer.p('\nCopying movie in kolekto tree...')
                movie_hash = copy(args.tree, filename)
            printer.p('')

            mdb.save(movie_hash, movie)
            printer.debug('Movie {hash} saved to the database', hash=movie_hash)

            if args.delete:
                os.unlink(filename)
                printer.debug('Deleted original file {filename}', filename=filename)

            # Import the attachments
            if movie_hash is not None and args.import_attachments:
                attachments = list_attachments(filename)
                if attachments:
                    printer.p('Found {nb} attachment(s) for this movie:', nb=len(attachments))
                    for attach in attachments:
                        printer.p(' - {filename}', filename=attach)
                    if not args.auto and printer.ask('Import them?', default=True):
                        for attach in attachments:
                            _, ext = os.path.splitext(attach)
                            attachment_store.store(movie_hash, ext.lstrip('.'), open(attach))

    def _import(self):
        raise NotImplementedError()


class ImportMovies(BaseImport):

    """ Import movies into the Kolekto tree.
    """

    help = 'import a movie'

    def _import(self, mdb, mds, args, config, filename):
        printer.debug('Importing file {filename}', filename=filename)
        short_filename = os.path.basename(filename)
        title, ext = os.path.splitext(short_filename)

        year, title = clean_title(title)

        # Disable the year filter if auto mode is disabled:
        if not args.auto:
            year = None

        while True:
            # Disable the title input if auto mode is enabled:
            if not args.auto:
                title = printer.input('Title to search', default=title)
            datasource, movie = self._search(mds, title, short_filename, year, auto=args.auto)
            if datasource == 'manual':
                movie = self.profile.object_class()
            elif datasource == 'abort':
                printer.p('Aborted import of {filename}', filename=filename)
                return
            break

        if datasource is None:
            return

        return movie

    def _search(self, mdb, query, filename, year=None, auto=False):
        """ Search the movie using all available datasources and let the user
            select a result. Return the choosen datasource and produced movie dict.

        If auto is enabled, directly returns the first movie found.
        """
        choices = []
        for datasource, movie in mdb.search(query, year=year):
            if auto:
                return datasource, movie
            if movie.get('directors'):
                directors = ' by '
                if len(movie['directors']) > 1:
                    directors += '%s and %s' % (', '.join(movie['directors'][0:-1]),
                                                          movie['directors'][-1])
                else:
                    directors += movie['directors'][0]
            else:
                directors = ''
            fmt = '<b>{title}</b> ({year}){directors} [{datasource}]'
            choices.append(option((datasource, movie), fmt, title=movie['title'],
                                                            year=movie.get('year', 'Unknown'),
                                                            directors=directors,
                                                            datasource=datasource.name))

        if not choices:
            printer.p('No results to display for the file: {fn}', fn=filename)
            return None, None

        choices.append(option(('manual', None), 'Enter information manually'))
        choices.append(option(('abort', None), 'None of these'))
        printer.p('Please choose the relevant movie for the file: {fn}', fn=filename, end='\n\n')
        return printer.choice(choices)


class ImportTvSeries(BaseImport):

    """ Import TV Series into the Kolekto tree.
    """

    help = 'import a tv series episode'

    def _import(self, mdb, mds, args, config, filename):
        printer.debug('Importing file {filename}', filename=filename)
        short_filename = os.path.basename(filename)
        title, ext = os.path.splitext(short_filename)

        title, season_num, episode_num = clean_title_series(title)

        while True:
            # Disable the title input if auto mode is enabled:
            if not args.auto:
                title = printer.input('Title to search', default=title)
                season_num = int(printer.input('Season number', default=season_num))
                episode_num = int(printer.input('Episode number', default=episode_num))
            datasource, episode = self._search(mds, title, short_filename, season_num, episode_num, auto=args.auto)
            if datasource == 'manual':
                episode = self.profile.object_class()
            elif datasource == 'abort':
                printer.p('Aborted import of {filename}', filename=filename)
                return
            break

        if datasource is None:
            return

        return episode

        # Refresh the full data for the choosen movie:
        episode = mds.refresh(episode)

    def _search(self, mdb, query, filename, season_num, episode_num, auto=False):
        """ Search the movie using all available datasources and let the user
            select a result. Return the choosen datasource and produced movie dict.

        If auto is enabled, directly returns the first movie found.
        """
        choices = []
        for datasource, movie in mdb.search(query, season=season_num, episode=episode_num):
            if auto:
                return datasource, movie
            fmt = '<b>{title}</b> - <b>{ep}</b> S{season:02d}E{episode:02d} [{datasource}]'
            choices.append(option((datasource, movie), fmt, title=movie['title'],
                                                            ep=movie['episode_title'],
                                                            season=movie['season'],
                                                            episode=movie['episode'],
                                                            datasource=datasource.name))

        if not choices:
            printer.p('No results to display for the file: {fn}', fn=filename)
            return None, None

        choices.append(option(('manual', None), 'Enter information manually'))
        choices.append(option(('abort', None), 'None of these'))
        printer.p('Please choose the relevant result for the file: {fn}', fn=filename, end='\n\n')
        return printer.choice(choices)
