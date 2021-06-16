.. Copyright (C) 2017 David Pinto <david.pinto@bioch.ox.ac.uk>

   Permission is granted to copy, distribute and/or modify this
   document under the terms of the GNU Free Documentation License,
   Version 1.3 or any later version published by the Free Software
   Foundation; with no Invariant Sections, no Front-Cover Texts, and
   no Back-Cover Texts.  A copy of the license is included in the
   section entitled "GNU Free Documentation License".

Contributor instructions
************************

Tests
=====

Microscope testsuite can be ran with setuptools, like so::

    python setup.py test

All test units, as well as other tools for testing purposes, are part
of the :py:mod:`microscope.testsuite` package.


Documentation
=============

Documentation is inline within docstrings and must follow the `Google
Python docstrings format
<http://google.github.io/styleguide/pyguide.html?showone=Comments#Comments>`_.
These will be parsed by `Sphinx's Napoleon extension
<http://www.sphinx-doc.org/en/stable/ext/napoleon.html>`_ so `its own
examples
<http://www.sphinx-doc.org/en/stable/ext/example_google.html>`_.  This
can be generated like so::

      python setup.py build_sphinx


Versioning
==========

We use the style `major.minor.patch` for releases and haven't yet had
to deal with rc.

In between releases and snapshots, we use the `dev` as a local version
identifiers as per `PEP 440
<https://www.python.org/dev/peps/pep-0440/>`_ so a version string
`0.0.1+dev` is the release `0.0.1` plus development changes on top of
it (and not development release for an upcoming `0.0.1`).  With
examples:

* `0.0.1` - major version 0, minor version 0, patch version 1

* `0.0.1+dev` - not a public release.  A development build, probably
  from VCS sources, sometime *after* release `0.0.1`.  Note the use of
  `+` which marks `dev` as a local version identifier.

* `0.0.1.dev1` - we do not do this.  PEP 440 states this would be the
  first development public release *before* `0.0.1`.  We use `+dev`
  which are local version and not public releases.  This is only
  mentioned here to avoid confusion for people used to that style.


Steps to make a release
=======================

#. Change version number on `setup.py` and commit that change only.
   Note that we use `release-N` for tag and not `v.N` to allow one day
   for snapshot releases with tags such as `snapshot-N`::

    VERSION=$(python setup.py --version)
    COMMIT=$(git rev-parse HEAD | cut -c1-12)
    git commit -m "maint: release $VERSION" setup.py
    git tag -s -u $GPGKEY -m \
      "Added tag release-$VERSION for commit $COMMIT" release-$VERSION

#. Build a source distribution from an export (in case of any non
   committed or ignored files)::

    rm -rf target/
    git archive --format=tar --prefix="target/" HEAD | tar -x
    cd target/
    python setup.py sdist --formats=gztar

#. Upload and sign distribution::

    twine upload -r pypi -s -i $GPGKEY target/dist/microscope-X.tar.gz

#. Add `+dev` to version string::

    sed -i "s,\(^project_version = '[^+]*\)',\1+dev'," setup.py
    git commit setup.py -m \
      "maint: set version to $VERSION+dev after $VERSION release."
    git push upstream master
    git push upstream release-$VERSION
