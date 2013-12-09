"""
Spack allows very fine-grained control over how packages are installed and
over how they are built and configured.  To make this easy, it has its own
syntax for declaring a dependence.  We call a descriptor of a particular
package configuration a "spec".

The syntax looks like this:

    spack install mpileaks ^openmpi @1.2:1.4 +debug %intel @12.1
                  0        1        2        3      4      5

The first part of this is the command, 'spack install'.  The rest of the
line is a spec for a particular installation of the mpileaks package.

0. The package to install

1. A dependency of the package, prefixed by ^

2. A version descriptor for the package.  This can either be a specific
   version, like "1.2", or it can be a range of versions, e.g. "1.2:1.4".
   If multiple specific versions or multiple ranges are acceptable, they
   can be separated by commas, e.g. if a package will only build with
   versions 1.0, 1.2-1.4, and 1.6-1.8 of mavpich, you could say:

       depends_on("mvapich@1.0,1.2:1.4,1.6:1.8")

3. A compile-time variant of the package.  If you need openmpi to be
   built in debug mode for your package to work, you can require it by
   adding +debug to the openmpi spec when you depend on it.  If you do
   NOT want the debug option to be enabled, then replace this with -debug.

4. The name of the compiler to build with.

5. The versions of the compiler to build with.  Note that the identifier
   for a compiler version is the same '@' that is used for a package version.
   A version list denoted by '@' is associated with the compiler only if
   if it comes immediately after the compiler name.  Otherwise it will be
   associated with the current package spec.

Here is the EBNF grammar for a spec:

  spec-list    = { spec [ dep-list ] }
  dep_list     = { ^ spec }
  spec         = id [ options ]
  options      = { @version-list | +variant | -variant | ~variant |
                   %compiler | =architecture }
  variant      = id
  architecture = id
  compiler     = id [ version-list ]
  version-list = version [ { , version } ]
  version      = id | id: | :id | id:id
  id           = [A-Za-z0-9_][A-Za-z0-9_.-]*

There is one context-sensitive part: ids in versions may contain '.', while
other ids may not.

There is one ambiguity: since '-' is allowed in an id, you need to put
whitespace space before -variant for it to be tokenized properly.  You can
either use whitespace, or you can just use ~variant since it means the same
thing.  Spack uses ~variant in directory names and in the canonical form of
specs to avoid ambiguity.  Both are provided because ~ can cause shell
expansion when it is the first character in an id typed on the command line.
"""
import sys
from StringIO import StringIO

import tty
import spack.parse
import spack.error
import spack.compilers
import spack.compilers.gcc
import spack.packages as packages
import spack.arch as arch

from spack.version import *
from spack.color import *
from spack.util.lang import *
from spack.util.string import *


"""This map determines the coloring of specs when using color output.
   We make the fields different colors to enhance readability.
   See spack.color for descriptions of the color codes.
"""
color_formats = {'%' : '@g',   # compiler
                 '@' : '@c',   # version
                 '=' : '@m',   # architecture
                 '+' : '@B',   # enable variant
                 '~' : '@r',   # disable variant
                 '^' : '@.'}   # dependency

"""Regex used for splitting by spec field separators."""
separators = '[%s]' % ''.join(color_formats.keys())


def index_specs(specs):
    """Take a list of specs and return a dict of lists.  Dict is
       keyed by spec name and lists include all specs with the
       same name.
    """
    spec_dict = {}
    for spec in specs:
        if not spec.name in spec_dict:
            spec_dict[spec.name] = []
        spec_dict[spec.name].append(spec)
    return spec_dict


def colorize_spec(spec):
    """Returns a spec colorized according to the colors specified in
       color_formats."""
    class insert_color:
        def __init__(self):
            self.last = None

        def __call__(self, match):
            # ignore compiler versions (color same as compiler)
            sep = match.group(0)
            if self.last == '%' and sep == '@':
                return cescape(sep)
            self.last = sep

            return '%s%s' % (color_formats[sep], cescape(sep))

    return colorize(re.sub(separators, insert_color(), str(spec)) + '@.')


@key_ordering
class Compiler(object):
    """The Compiler field represents the compiler or range of compiler
       versions that a package should be built with.  Compilers have a
       name and a version list.
    """
    def __init__(self, name, version=None):
        if name not in spack.compilers.supported_compilers():
            raise UnknownCompilerError(name)

        self.name = name
        self.versions = VersionList()
        if version:
            self.versions.add(version)


    def _add_version(self, version):
        self.versions.add(version)


    def satisfies(self, other):
        return (self.name == other.name and
                self.versions.overlaps(other.versions))


    def constrain(self, other):
        if not self.satisfies(other):
            raise UnsatisfiableCompilerSpecError(self, other)

        self.versions.intersect(other.versions)


    @property
    def concrete(self):
        """A Compiler spec is concrete if its versions are concrete."""
        return self.versions.concrete


    @property
    def version(self):
        if not self.concrete:
            raise SpecError("Spec is not concrete: " + str(self))
        return self.versions[0]


    def copy(self):
        clone = Compiler(self.name)
        clone.versions = self.versions.copy()
        return clone


    def _cmp_key(self):
        return (self.name, self.versions)


    def __str__(self):
        out = self.name
        if self.versions:
            vlist = ",".join(str(v) for v in self.versions)
            out += "@%s" % vlist
        return out


@key_ordering
class Variant(object):
    """Variants are named, build-time options for a package.  Names depend
       on the particular package being built, and each named variant can
       be enabled or disabled.
    """
    def __init__(self, name, enabled):
        self.name = name
        self.enabled = enabled


    def _cmp_key(self):
        return (self.name, self.enabled)


    def copy(self):
        return Variant(self.name, self.enabled)


    def __str__(self):
        out = '+' if self.enabled else '~'
        return out + self.name


class VariantMap(HashableMap):
    def satisfies(self, other):
        return all(self[key].enabled == other[key].enabled
                   for key in other if key in self)


    def __str__(self):
        sorted_keys = sorted(self.keys())
        return ''.join(str(self[key]) for key in sorted_keys)


class DependencyMap(HashableMap):
    """Each spec has a DependencyMap containing specs for its dependencies.
       The DependencyMap is keyed by name. """
    @property
    def concrete(self):
        return all(d.concrete for d in self.values())


    def satisfies(self, other):
        return all(self[name].satisfies(other[name]) for name in self
                   if name in other)


    def __str__(self):
        sorted_dep_names = sorted(self.keys())
        return ''.join(
            ["^" + str(self[name]) for name in sorted_dep_names])


@key_ordering
class Spec(object):
    def __init__(self, spec_like, *dep_like):
        # Copy if spec_like is a Spec.
        if type(spec_like) == Spec:
            self._dup(spec_like)
            return

        # Parse if the spec_like is a string.
        if type(spec_like) != str:
            raise TypeError("Can't make spec out of %s" % type(spec_like))

        spec_list = SpecParser().parse(spec_like)
        if len(spec_list) > 1:
            raise ValueError("More than one spec in string: " + spec_like)
        if len(spec_list) < 1:
            raise ValueError("String contains no specs: " + spec_like)

        # Take all the attributes from the first parsed spec without copying.
        # This is safe b/c we throw out the parsed spec.  It's a bit nasty,
        # but it's nastier to implement the constructor so that the parser
        # writes directly into this Spec object.
        other = spec_list[0]
        self.name = other.name
        self.dependents = other.dependents
        self.versions = other.versions
        self.variants = other.variants
        self.architecture = other.architecture
        self.compiler = other.compiler
        self.dependencies = other.dependencies

        # This allows users to construct a spec DAG with literals.
        # Note that given two specs a and b, Spec(a) copies a, but
        # Spec(a, b) will copy a but just add b as a dep.
        for dep in dep_like:
            spec = dep if type(dep) == Spec else Spec(dep)
            self._add_dependency(spec)


    #
    # Private routines here are called by the parser when building a spec.
    #
    def _add_version(self, version):
        """Called by the parser to add an allowable version."""
        self.versions.add(version)


    def _add_variant(self, name, enabled):
        """Called by the parser to add a variant."""
        if name in self.variants: raise DuplicateVariantError(
                "Cannot specify variant '%s' twice" % name)
        self.variants[name] = Variant(name, enabled)


    def _set_compiler(self, compiler):
        """Called by the parser to set the compiler."""
        if self.compiler: raise DuplicateCompilerError(
                "Spec for '%s' cannot have two compilers." % self.name)
        self.compiler = compiler


    def _set_architecture(self, architecture):
        """Called by the parser to set the architecture."""
        if self.architecture: raise DuplicateArchitectureError(
                "Spec for '%s' cannot have two architectures." % self.name)
        self.architecture = architecture


    def _add_dependency(self, spec):
        """Called by the parser to add another spec as a dependency."""
        if spec.name in self.dependencies:
            raise DuplicateDependencyError("Cannot depend on '%s' twice" % spec)
        self.dependencies[spec.name] = spec
        spec.dependents[self.name] = self


    @property
    def root(self):
        """Follow dependent links and find the root of this spec's DAG.
           In spack specs, there should be a single root (the package being
           installed).  This will throw an assertion error if that is not
           the case.
        """
        if not self.dependents:
            return self
        else:
            # If the spec has multiple dependents, ensure that they all
            # lead to the same place.  Spack shouldn't deal with any DAGs
            # with multiple roots, so something's wrong if we find one.
            depiter = iter(self.dependents.values())
            first_root = next(depiter).root
            assert(all(first_root is d.root for d in depiter))
            return first_root


    @property
    def package(self):
        if self.virtual:
            raise TypeError("Cannot get package for virtual spec '" +
                            self.name + "'")
        return packages.get(self.name)


    @property
    def virtual(self):
        """Right now, a spec is virtual if no package exists with its name.
           TODO: revisit this -- might need to use a separate namespace and
                 be more explicit about this.
                 Possible idea: just use conventin and make virtual deps all
                 caps, e.g., MPI vs mpi.
        """
        return not packages.exists(self.name)


    @property
    def concrete(self):
        """A spec is concrete if it can describe only ONE build of a package.
           If any of the name, version, architecture, compiler, or depdenencies
           are ambiguous,then it is not concrete.
        """
        return bool(not self.virtual
                    and self.versions.concrete
                    and self.architecture
                    and self.compiler and self.compiler.concrete
                    and self.dependencies.concrete)


    def preorder_traversal(self, visited=None, d=0, **kwargs):
        """Generic preorder traversal of the DAG represented by this spec.
           This will yield each node in the spec.  Options:

           cover    [=nodes|edges|paths]
               Determines how extensively to cover the dag.  Possible vlaues:

               'nodes': Visit each node in the dag only once.  Every node
                        yielded by this function will be unique.
               'edges': If a node has been visited once but is reached along a
                        new path from the root, yield it but do not descend
                        into it.  This traverses each 'edge' in the DAG once.
               'paths': Explore every unique path reachable from the root.
                        This descends into visited subtrees and will yield
                        nodes twice if they're reachable by multiple paths.

           depth    [=False]
               Defaults to False.  When True, yields not just nodes in the
               spec, but also their depth from the root in a (depth, node)
               tuple.

           keyfun   [=id]
               Allow a custom key function to track the identity of nodes
               in the traversal.

           root     [=True]
               If false, this won't yield the root node, just its descendents.
        """
        depth      = kwargs.setdefault('depth', False)
        key_fun    = kwargs.setdefault('key', id)
        yield_root = kwargs.setdefault('root', True)
        cover      = kwargs.setdefault('cover', 'nodes')

        cover_values = ('nodes', 'edges', 'paths')
        if cover not in cover_values:
            raise ValueError("Invalid value for cover: %s.  Choices are %s"
                             % (cover, ",".join(cover_values)))

        if visited is None:
            visited = set()

        result = (d, self) if depth else self
        key = key_fun(self)

        if key in visited:
            if cover == 'nodes':    return
            if yield_root or d > 0: yield result
            if cover == 'edges':    return
        else:
            if yield_root or d > 0: yield result

        visited.add(key)
        for name in sorted(self.dependencies):
            child = self.dependencies[name]
            for elt in child.preorder_traversal(visited, d+1, **kwargs):
                yield elt


    def _concretize_helper(self, presets=None, visited=None):
        """Recursive helper function for concretize().
           This concretizes everything bottom-up.  As things are
           concretized, they're added to the presets, and ancestors
           will prefer the settings of their children.
        """
        if presets is None: presets = {}
        if visited is None: visited = set()

        if self.name in visited:
            return

        # Concretize deps first -- this is a bottom-up process.
        for name in sorted(self.dependencies.keys()):
            self.dependencies[name]._concretize_helper(presets, visited)

        if self.name in presets:
            self.constrain(presets[self.name])
        else:
            # Concretize virtual dependencies last.  Because they're added
            # to presets below, their constraints will all be merged, but we'll
            # still need to select a concrete package later.
            if not self.virtual:
                spack.concretizer.concretize_architecture(self)
                spack.concretizer.concretize_compiler(self)
                spack.concretizer.concretize_version(self)
            presets[self.name] = self

        visited.add(self.name)


    def _expand_virtual_packages(self):
        """Find virtual packages in this spec, replace them with providers,
           and normalize again to include the provider's (potentially virtual)
           dependencies.  Repeat until there are no virtual deps.

           TODO: If a provider depends on something that conflicts with
                 other dependencies in the spec being expanded, this can
                 produce a conflicting spec.  For example, if mpich depends
                 on hwloc@:1.3 but something in the spec needs hwloc1.4:,
                 then we should choose an MPI other than mpich.  Cases like
                 this are infrequent, but should implement this before it is
                 a problem.
        """
        while True:
            virtuals =[v for v in self.preorder_traversal() if v.virtual]
            if not virtuals:
                return

            for spec in virtuals:
                providers = packages.providers_for(spec)
                concrete = spack.concretizer.choose_provider(spec, providers)
                concrete = concrete.copy()

                for name, dependent in spec.dependents.items():
                    del dependent.dependencies[spec.name]
                    dependent._add_dependency(concrete)

            # If there are duplicate providers or duplicate provider deps, this
            # consolidates them and merges constraints.
            self.normalize()


    def concretize(self):
        """A spec is concrete if it describes one build of a package uniquely.
           This will ensure that this spec is concrete.

           If this spec could describe more than one version, variant, or build
           of a package, this will add constraints to make it concrete.

           Some rigorous validation and checks are also performed on the spec.
           Concretizing ensures that it is self-consistent and that it's consistent
           with requirements of its pacakges.  See flatten() and normalize() for
           more details on this.
        """
        self.normalize()
        self._expand_virtual_packages()
        self._concretize_helper()


    def concretized(self):
        """This is a non-destructive version of concretize().  First clones,
           then returns a concrete version of this package without modifying
           this package. """
        clone = self.copy()
        clone.concretize()
        return clone


    def flat_dependencies(self):
        """Return a DependencyMap containing all of this spec's dependencies
           with their constraints merged.  If there are any conflicts, throw
           an exception.

           This will work even on specs that are not normalized; i.e. specs
           that have two instances of the same dependency in the DAG.
           This is used as the first step of normalization.
        """
        # This ensures that the package descriptions themselves are consistent
        if not self.virtual:
            self.package.validate_dependencies()

        # Once that is guaranteed, we know any constraint violations are due
        # to the spec -- so they're the user's fault, not Spack's.
        flat_deps = DependencyMap()
        try:
            for spec in self.preorder_traversal():
                if spec.name not in flat_deps:
                    new_spec = spec.copy(dependencies=False)
                    flat_deps[spec.name] = new_spec

                else:
                    flat_deps[spec.name].constrain(spec)

        except UnsatisfiableSpecError, e:
            # This REALLY shouldn't happen unless something is wrong in spack.
            # It means we got a spec DAG with two instances of the same package
            # that had inconsistent constraints.  There's no way for a user to
            # produce a spec like this (the parser adds all deps to the root),
            # so this means OUR code is not sane!
            raise InconsistentSpecError("Invalid Spec DAG: %s" % e.message)

        return flat_deps


    def flatten(self):
        """Pull all dependencies up to the root (this spec).
           Merge constraints for dependencies with the same name, and if they
           conflict, throw an exception. """
        self.dependencies = self.flat_dependencies()


    def _normalize_helper(self, visited, spec_deps, provider_index):
        """Recursive helper function for _normalize."""
        if self.name in visited:
            return
        visited.add(self.name)

        # if we descend into a virtual spec, there's nothing more
        # to normalize.  Concretize will finish resolving it later.
        if self.virtual:
            return

        # Combine constraints from package dependencies with
        # constraints on the spec's dependencies.
        pkg = packages.get(self.name)
        for name, pkg_dep in self.package.dependencies.items():
            # If it's a virtual dependency, try to find a provider
            if pkg_dep.virtual:
                providers = provider_index.providers_for(pkg_dep)

                # If there is a provider for the vpkg, then use that instead of
                # the virtual package.
                if providers:
                    # Can't have multiple providers for the same thing in one spec.
                    if len(providers) > 1:
                        raise MultipleProviderError(pkg_dep, providers)

                    pkg_dep = providers[0]
                    name    = pkg_dep.name

                else:
                    # The user might have required something insufficient for
                    # pkg_dep -- so we'll get a conflict.  e.g., user asked for
                    # mpi@:1.1 but some package required mpi@2.1:.
                    providers = provider_index.providers_for(name)
                    if len(providers) > 1:
                        raise MultipleProviderError(pkg_dep, providers)
                    if providers:
                        raise UnsatisfiableProviderSpecError(providers[0], pkg_dep)


            if name not in spec_deps:
                # If the spec doesn't reference a dependency that this package
                # needs, then clone it from the package description.
                spec_deps[name] = pkg_dep.copy()

            try:
                # Constrain package information with spec info
                spec_deps[name].constrain(pkg_dep)

            except UnsatisfiableSpecError, e:
                e.message =  "Invalid spec: '%s'. "
                e.message += "Package %s requires %s %s, but spec asked for %s"
                e.message %= (spec_deps[name], name, e.constraint_type,
                              e.required, e.provided)
                raise e

            # Add merged spec to my deps and recurse
            dependency = spec_deps[name]
            self._add_dependency(dependency)
            dependency._normalize_helper(visited, spec_deps, provider_index)


    def normalize(self):
        """When specs are parsed, any dependencies specified are hanging off
           the root, and ONLY the ones that were explicitly provided are there.
           Normalization turns a partial flat spec into a DAG, where:
             1) ALL dependencies of the root package are in the DAG.
             2) Each node's dependencies dict only contains its direct deps.
             3) There is only ONE unique spec for each package in the DAG.
                - This includes virtual packages.  If there a non-virtual
                  package that provides a virtual package that is in the spec,
                  then we replace the virtual package with the non-virtual one.
             4) The spec DAG matches package DAG.
        """
        # Ensure first that all packages in the DAG exist.
        self.validate_package_names()

        # Then ensure that the packages referenced are sane, that the
        # provided spec is sane, and that all dependency specs are in the
        # root node of the spec.  flat_dependencies will do this for us.
        spec_deps = self.flat_dependencies()
        self.dependencies.clear()

        # Figure out which of the user-provided deps provide virtual deps.
        # Remove virtual deps that are already provided by something in the spec
        spec_packages = [d.package for d in spec_deps.values() if not d.virtual]

        index = packages.ProviderIndex(spec_deps.values(), restrict=True)
        visited = set()
        self._normalize_helper(visited, spec_deps, index)

        # If there are deps specified but not visited, they're not
        # actually deps of this package.  Raise an error.
        extra = set(spec_deps.viewkeys()).difference(visited)

        # Also subtract out all the packags that provide a needed vpkg
        vdeps = [v for v in self.package.virtual_dependencies()]

        vpkg_providers = index.providers_for(*vdeps)
        extra.difference_update(p.name for p in vpkg_providers)

        # Anything left over is not a valid part of the spec.
        if extra:
            raise InvalidDependencyException(
                self.name + " does not depend on " + comma_or(extra))


    def validate_package_names(self):
        for spec in self.preorder_traversal():
            # Don't get a package for a virtual name.
            if not spec.virtual:
                packages.get(spec.name)


    def constrain(self, other):
        if not self.versions.overlaps(other.versions):
            raise UnsatisfiableVersionSpecError(self.versions, other.versions)

        for v in other.variants:
            if (v in self.variants and
                self.variants[v].enabled != other.variants[v].enabled):
                raise UnsatisfiableVariantSpecError(self.variants[v],
                                                    other.variants[v])

        if self.architecture is not None and other.architecture is not None:
            if self.architecture != other.architecture:
                raise UnsatisfiableArchitectureSpecError(self.architecture,
                                                         other.architecture)

        if self.compiler is not None and other.compiler is not None:
            self.compiler.constrain(other.compiler)
        elif self.compiler is None:
            self.compiler = other.compiler

        self.versions.intersect(other.versions)
        self.variants.update(other.variants)
        self.architecture = self.architecture or other.architecture


    def satisfies(self, other):
        if type(other) != Spec:
            other = Spec(other)

        def sat(attribute):
            s = getattr(self, attribute)
            o = getattr(other, attribute)
            return not s or not o or s.satisfies(o)

        return (self.name == other.name and
                all(sat(attr) for attr in
                    ('versions', 'variants', 'compiler', 'architecture')) and
                # TODO: what does it mean to satisfy deps?
                self.dependencies.satisfies(other.dependencies))


    def _dup(self, other, **kwargs):
        """Copy the spec other into self.  This is an overwriting
           copy.  It does not copy any dependents (parents), but by default
           copies dependencies.

           To duplicate an entire DAG, call _dup() on the root of the DAG.

           Options:
           dependencies[=True]
               Whether deps should be copied too.  Set to false to copy a
               spec but not its dependencies.
        """
        # TODO: this needs to handle DAGs.
        self.name = other.name
        self.versions = other.versions.copy()
        self.variants = other.variants.copy()
        self.architecture = other.architecture
        self.compiler = None
        if other.compiler:
            self.compiler = other.compiler.copy()

        self.dependents = DependencyMap()
        copy_deps = kwargs.get('dependencies', True)
        if copy_deps:
            self.dependencies = other.dependencies.copy()
        else:
            self.dependencies = DependencyMap()


    def copy(self, **kwargs):
        """Return a copy of this spec.
           By default, returns a deep copy.  Supply dependencies=False
           to get a shallow copy.
        """
        clone = Spec.__new__(Spec)
        clone._dup(self, **kwargs)
        return clone


    @property
    def version(self):
        if not self.concrete:
            raise SpecError("Spec is not concrete: " + str(self))
        return self.versions[0]


    def __getitem__(self, name):
        """TODO: does the way this is written make sense?"""
        for spec in self.preorder_traversal():
            if spec.name == name:
                return spec

        raise KeyError("No spec with name %s in %s" % (name, self))


    def __contains__(self, spec):
        """True if this spec has any dependency that satisfies the supplied
           spec."""
        if type(spec) != Spec:
            spec = Spec(spec)

        for s in self.preorder_traversal():
            if s.satisfies(spec):
                return True
        return False


    def _cmp_key(self):
        return (self.name, self.versions, self.variants,
                self.architecture, self.compiler, self.dependencies)


    def colorized(self):
        return colorize_spec(self)


    def str_no_deps(self, **kwargs):
        out = self.name

        # If the version range is entirely open, omit it
        if self.versions and self.versions != VersionList([':']):
            out += "@%s" % self.versions

        if self.compiler:
            out += "%%%s" % self.compiler

        out += str(self.variants)

        if self.architecture:
            out += "=%s" % self.architecture

        if kwargs.get('color', False):
            return colorize_spec(out)
        else:
            return out


    def tree(self, **kwargs):
        """Prints out this spec and its dependencies, tree-formatted
           with indentation."""
        color = kwargs.get('color', False)
        depth = kwargs.get('depth', False)
        cover = kwargs.get('cover', 'paths')

        out = ""
        cur_id = 0
        ids = {}
        for d, node in self.preorder_traversal(cover=cover, depth=True):
            if depth:
                out += "%-4d" % d
            if not id(node) in ids:
                cur_id += 1
                ids[id(node)] = cur_id
            out += "%-4d" % ids[id(node)]
            out += ("    " * d)
            out += node.str_no_deps(color=color) + "\n"
        return out


    def __repr__(self):
        return str(self)


    def __str__(self):
        byname = lambda d: d.name
        deps = self.preorder_traversal(key=byname, root=False)
        sorted_deps = sorted(deps, key=byname)
        dep_string = ''.join("^" + dep.str_no_deps() for dep in sorted_deps)
        return self.str_no_deps() + dep_string


#
# These are possible token types in the spec grammar.
#
DEP, AT, COLON, COMMA, ON, OFF, PCT, EQ, ID = range(9)

class SpecLexer(spack.parse.Lexer):
    """Parses tokens that make up spack specs."""
    def __init__(self):
        super(SpecLexer, self).__init__([
            (r'\^',        lambda scanner, val: self.token(DEP,   val)),
            (r'\@',        lambda scanner, val: self.token(AT,    val)),
            (r'\:',        lambda scanner, val: self.token(COLON, val)),
            (r'\,',        lambda scanner, val: self.token(COMMA, val)),
            (r'\+',        lambda scanner, val: self.token(ON,    val)),
            (r'\-',        lambda scanner, val: self.token(OFF,   val)),
            (r'\~',        lambda scanner, val: self.token(OFF,   val)),
            (r'\%',        lambda scanner, val: self.token(PCT,   val)),
            (r'\=',        lambda scanner, val: self.token(EQ,    val)),
            (r'\w[\w.-]*', lambda scanner, val: self.token(ID,    val)),
            (r'\s+',       lambda scanner, val: None)])


class SpecParser(spack.parse.Parser):
    def __init__(self):
        super(SpecParser, self).__init__(SpecLexer())


    def do_parse(self):
        specs = []
        while self.next:
            if self.accept(ID):
                specs.append(self.spec())

            elif self.accept(DEP):
                if not specs:
                    self.last_token_error("Dependency has no package")
                self.expect(ID)
                specs[-1]._add_dependency(self.spec())

            else:
                self.unexpected_token()

        return specs


    def spec(self):
        """Parse a spec out of the input.  If a spec is supplied, then initialize
           and return it instead of creating a new one."""
        self.check_identifier()

        # This will init the spec without calling __init__.
        spec = Spec.__new__(Spec)
        spec.name = self.token.value
        spec.versions = VersionList()
        spec.variants = VariantMap()
        spec.architecture = None
        spec.compiler = None
        spec.dependents   = DependencyMap()
        spec.dependencies = DependencyMap()

        # record this so that we know whether version is
        # unspecified or not.
        added_version = False

        while self.next:
            if self.accept(AT):
                vlist = self.version_list()
                for version in vlist:
                    spec._add_version(version)
                added_version = True

            elif self.accept(ON):
                spec._add_variant(self.variant(), True)

            elif self.accept(OFF):
                spec._add_variant(self.variant(), False)

            elif self.accept(PCT):
                spec._set_compiler(self.compiler())

            elif self.accept(EQ):
                spec._set_architecture(self.architecture())

            else:
                break

        # If there was no version in the spec, consier it an open range
        if not added_version:
            spec.versions = VersionList(':')

        return spec


    def variant(self):
        self.expect(ID)
        self.check_identifier()
        return self.token.value


    def architecture(self):
        self.expect(ID)
        self.check_identifier()
        return self.token.value


    def version(self):
        start = None
        end = None
        if self.accept(ID):
            start = self.token.value

        if self.accept(COLON):
            if self.accept(ID):
                end = self.token.value
        elif start:
            # No colon, but there was a version.
            return Version(start)
        else:
            # No colon and no id: invalid version.
            self.next_token_error("Invalid version specifier")

        if start: start = Version(start)
        if end: end = Version(end)
        return VersionRange(start, end)


    def version_list(self):
        vlist = []
        vlist.append(self.version())
        while self.accept(COMMA):
            vlist.append(self.version())
        return vlist


    def compiler(self):
        self.expect(ID)
        self.check_identifier()
        compiler = Compiler(self.token.value)
        if self.accept(AT):
            vlist = self.version_list()
            for version in vlist:
                compiler._add_version(version)
        return compiler


    def check_identifier(self):
        """The only identifiers that can contain '.' are versions, but version
           ids are context-sensitive so we have to check on a case-by-case
           basis. Call this if we detect a version id where it shouldn't be.
        """
        if '.' in self.token.value:
            self.last_token_error("Identifier cannot contain '.'")


def parse(string):
    """Returns a list of specs from an input string.
       For creating one spec, see Spec() constructor.
    """
    return SpecParser().parse(string)


class SpecError(spack.error.SpackError):
    """Superclass for all errors that occur while constructing specs."""
    def __init__(self, message):
        super(SpecError, self).__init__(message)


class DuplicateDependencyError(SpecError):
    """Raised when the same dependency occurs in a spec twice."""
    def __init__(self, message):
        super(DuplicateDependencyError, self).__init__(message)


class DuplicateVariantError(SpecError):
    """Raised when the same variant occurs in a spec twice."""
    def __init__(self, message):
        super(DuplicateVariantError, self).__init__(message)


class DuplicateCompilerError(SpecError):
    """Raised when the same compiler occurs in a spec twice."""
    def __init__(self, message):
        super(DuplicateCompilerError, self).__init__(message)


class UnknownCompilerError(SpecError):
    """Raised when the user asks for a compiler spack doesn't know about."""
    def __init__(self, compiler_name):
        super(UnknownCompilerError, self).__init__(
            "Unknown compiler: %s" % compiler_name)


class DuplicateArchitectureError(SpecError):
    """Raised when the same architecture occurs in a spec twice."""
    def __init__(self, message):
        super(DuplicateArchitectureError, self).__init__(message)


class InconsistentSpecError(SpecError):
    """Raised when two nodes in the same spec DAG have inconsistent
       constraints."""
    def __init__(self, message):
        super(InconsistentSpecError, self).__init__(message)


class InvalidDependencyException(SpecError):
    """Raised when a dependency in a spec is not actually a dependency
       of the package."""
    def __init__(self, message):
        super(InvalidDependencyException, self).__init__(message)


class NoProviderError(SpecError):
    """Raised when there is no package that provides a particular
       virtual dependency.
    """
    def __init__(self, vpkg):
        super(NoProviderError, self).__init__(
            "No providers found for virtual package: '%s'" % vpkg)
        self.vpkg = vpkg


class MultipleProviderError(SpecError):
    """Raised when there is no package that provides a particular
       virtual dependency.
    """
    def __init__(self, vpkg, providers):
        """Takes the name of the vpkg"""
        super(MultipleProviderError, self).__init__(
            "Multiple providers found for '%s': %s"
            % (vpkg, [str(s) for s in providers]))
        self.vpkg = vpkg
        self.providers = providers


class UnsatisfiableSpecError(SpecError):
    """Raised when a spec conflicts with package constraints.
       Provide the requirement that was violated when raising."""
    def __init__(self, provided, required, constraint_type):
        super(UnsatisfiableSpecError, self).__init__(
            "%s does not satisfy %s" % (provided, required))
        self.provided = provided
        self.required = required
        self.constraint_type = constraint_type


class UnsatisfiableVersionSpecError(UnsatisfiableSpecError):
    """Raised when a spec version conflicts with package constraints."""
    def __init__(self, provided, required):
        super(UnsatisfiableVersionSpecError, self).__init__(
            provided, required, "version")


class UnsatisfiableCompilerSpecError(UnsatisfiableSpecError):
    """Raised when a spec comiler conflicts with package constraints."""
    def __init__(self, provided, required):
        super(UnsatisfiableCompilerSpecError, self).__init__(
            provided, required, "compiler")


class UnsatisfiableVariantSpecError(UnsatisfiableSpecError):
    """Raised when a spec variant conflicts with package constraints."""
    def __init__(self, provided, required):
        super(UnsatisfiableVariantSpecError, self).__init__(
            provided, required, "variant")


class UnsatisfiableArchitectureSpecError(UnsatisfiableSpecError):
    """Raised when a spec architecture conflicts with package constraints."""
    def __init__(self, provided, required):
        super(UnsatisfiableArchitectureSpecError, self).__init__(
            provided, required, "architecture")


class UnsatisfiableProviderSpecError(UnsatisfiableSpecError):
    """Raised when a provider is supplied but constraints don't match
       a vpkg requirement"""
    def __init__(self, provided, required):
        super(UnsatisfiableProviderSpecError, self).__init__(
            provided, required, "provider")