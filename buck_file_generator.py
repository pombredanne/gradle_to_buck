import argparse
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.cElementTree as xml
import zipfile
from os import path

SRC_ROOTS_REGEX = re.compile(r'^\s*src_roots\s*=\s*(.*)$')
BUCK_FILE_TEMPLATE = """{library_type}(
  name = '{name}',
  srcs = {sources},
  deps = [
{deps}
  ],
  visibility = [
    'PUBLIC',
  ],
)

"""

ANDROID_RESOURCE_TEMPLATE = """android_resource(
  name = 'res',
  package = '{package}',
  res = 'res',
  deps = [
  ],
  visibility = [
    'PUBLIC',
  ],
)

"""

ANDROID_BUILD_CONFIG_TEMPLATE = """android_build_config(
  name = 'build-config',
  package = '{package}',
  visibility = [
    'PUBLIC',
  ],
)

"""

REMOTE_DEP_TEMPLATE = """
{prebuilt_type}(
  name = '{name}',
  {binary_field} = ':{name}-jar',
  visibility = [
    'PUBLIC',
  ],
)

remote_file(
  name = '{name}-jar',
  url = '{repo}:{coordinate}',
  sha1 = '{hash}',
)

"""

INTERFACE_FILES_TEMPLATE = """INTERFACE_FILES = [
{0}
]

"""

CYCLE_PREFIX = 'BUILD FAILED: Cycle found: '

THIRD_PARTY_JAR = re.compile(r"^\s*(?:\S*ompile|provided)\s*'(\S*)'$")
MAVEN_COORDINATE = re.compile(r"([^:]+):([^:]+):([^:]+:)?([^:]+)")

CLASS_FILE = re.compile(r'\s(\S+).class$')
JAVA_IMPORT = re.compile(r'import (.*);$')

NAME_DECLARATION = re.compile(r"\s*name\s=\s'(\S*)'.*")
DEP_DECLARATION = re.compile(r"\s*'(\S*)',")
DEPS_START = re.compile(r'\s*deps\s*=\s*\[$')

PACKAGE_DECLARATION = re.compile(r"\s*package\s=\s'(\S*)'.*")
FNULL = open(os.devnull, 'w')

INTERFACE_DECLARATION = re.compile(r'public\s+@?interface\s+.*')

POSSIBLE_MAVEN_TYPES = [('aar', 'android_prebuilt_aar', 'aar'),
                        ('jar', 'prebuilt_jar', 'binary_jar')]

INTERFACE_SUFFIX = '-interfaces'

BUCK_CONFIG_TEMPLATE = r"""[java]
    ; Indicates that any folder named src or test
    ; are folders that contain Java code.
    src_roots = {src_roots}
[project]
  ignore = \
    .git, \
    .buckd, \
    .gradle, \
    build, \
    proguard
  temp_files = \
    .*\.swp$, \
    ^#.*#$, .*~$, \
    .*___jb_bak___$, .*___jb_old___$, \
    .*\.ap_$
[cache]
  mode = dir
  dir = buck-cache
  dir_max_size = 10GB
[download]
  in_build = true

[maven_repositories]
  {maven_repositories}
"""

REPOSITORY_START = re.compile(r'repositories \{')
GRADLE_EXTERNAL_REPO = re.compile(
    'maven\\s+\\{\\s+url\\s+["\'](.*)["\']\\s+\\}')
REPOSITORY_MAP = {
    'jcenter': 'https://jcenter.bintray.com',
    'mavenCentral': 'https://repo1.maven.org/maven2',
}


def get_repositories_from_gradle_file(gradle_file_path):
    in_repositories = False
    result = set()
    with open(gradle_file_path, 'r') as gradle_file:
        for line in gradle_file.readlines():
            repository_start_match = REPOSITORY_START.search(line)
            if repository_start_match:
                in_repositories = True
            elif in_repositories:
                if line.strip().startswith('}'):
                    in_repositories = False
                else:
                    external_repo_match = GRADLE_EXTERNAL_REPO.search(line)
                    repo_function_name = line.strip().strip('()')
                    if external_repo_match:
                        result.add(external_repo_match.group(1))
                    elif repo_function_name in REPOSITORY_MAP:
                        result.add(REPOSITORY_MAP[repo_function_name])
    return result


def get_source_roots(buckconfig):
    src_roots = []
    with open(buckconfig, 'r') as buckconfig_file:
        for line in buckconfig_file.readlines():
            match = SRC_ROOTS_REGEX.match(line)
            if match:
                src_roots = map(str.strip, match.group(1).split(','))

    return src_roots


def format_deps_for_buck_file(deps):
    return sorted(("     '{0}',".format(dep) for dep in deps))


def is_interface_file(file):
    if not args.split_interfaces:
        return False
    with open(file, 'r') as java_file:
        for line in java_file.readlines():
            if INTERFACE_DECLARATION.match(line):
                return True
    return False


def get_interface_files(root, files):
    interface_files = set()
    for file in (x for x in files if x.endswith('.java')):
        if is_interface_file(path.join(root, file)):
            interface_files.add(file)
            break
    return interface_files


def get_deps_for_files(root,
                       files,
                       src_roots,
                       rule_name,
                       third_party_map,
                       android_libraries):
    deps = set()
    has_android_deps = False
    for file in (x for x in files if x.endswith('.java')):
        with open(path.join(root, file), 'r') as java_file:
            for line in java_file.readlines():
                match = JAVA_IMPORT.match(line)
                if match:
                    needed_class = match.group(1)
                    if (needed_class.startswith('android') or
                            needed_class.startswith('com.android')):
                        has_android_deps = True
                    if needed_class in third_party_map:
                        deps.add(third_party_map[needed_class])
                    else:
                        java_file = needed_class.replace('.', '/') + '.java'
                        for src_root in src_roots:
                            src_root = src_root.lstrip('/')
                            java_file_full_path = path.join(
                                src_root, java_file)
                            if path.exists(java_file_full_path):
                                target_basename = path.join(
                                    src_root,
                                    path.dirname(java_file))
                                rule_name = path.basename(
                                    path.dirname(java_file))
                                if is_interface_file(java_file_full_path):
                                    rule_name += INTERFACE_SUFFIX
                                target = '//{0}:{1}'.format(
                                    target_basename,
                                    rule_name)
                                if (path.abspath(target_basename) !=
                                        path.abspath(root)):
                                    deps.add(target)
                                    if target in android_libraries:
                                        has_android_deps = True
                                break
    if has_android_deps:
        android_libraries.add(rule_name)
    return deps, has_android_deps


def generate_default_buck_files(buckconfig,
                                src_roots,
                                third_party_map,
                                android_libraries,
                                default_library_type):
    buck_files = []
    for src_root in src_roots:
        src_root = src_root.lstrip('/')
        path_walker = os.walk(path.join(path.dirname(buckconfig), src_root))
        for root, dirs, files in path_walker:
            if 'BUCK' not in files and any((x for x in files if
                                            x.endswith('.java'))):
                interface_files = get_interface_files(root, files)
                with open(path.join(root, 'BUCK'), 'w') as buck_file:
                    if interface_files:
                        buck_file.write(INTERFACE_FILES_TEMPLATE.format(
                            ', \n'.join(("  '%s'" % x for x in
                                         interface_files))
                        ))
                        interface_rule = path.basename(root) + INTERFACE_SUFFIX
                        interface_buck_rule = '//{0}:{1}-interfaces'.format(
                            path.relpath(root),
                            path.basename(root))
                        interface_deps, has_android_deps = get_deps_for_files(
                            root,
                            interface_files,
                            src_roots,
                            interface_buck_rule,
                            third_party_map,
                            android_libraries)
                        interface_library_type = default_library_type
                        if has_android_deps:
                            interface_library_type = 'android_library'

                        buck_file.write(
                            BUCK_FILE_TEMPLATE.format(
                                library_type=interface_library_type,
                                sources='INTERFACE_FILES',
                                name=interface_rule,
                                deps='\n'.join(
                                    format_deps_for_buck_file(interface_deps))
                            ))
                        buck_files.append(interface_buck_rule)
                    main_buck_rule = '//{0}:{1}'.format(
                        path.relpath(root),
                        path.basename(root))
                    main_rule_deps, has_android_deps = get_deps_for_files(
                        root,
                        set(files).difference(
                            interface_files),
                        src_roots,
                        main_buck_rule,
                        third_party_map,
                        android_libraries)
                    main_library_type = default_library_type
                    if has_android_deps:
                        main_library_type = 'android_library'
                    main_rule_srcs = "glob(['*.java'])"
                    if interface_files:
                        main_rule_srcs = "glob(['*.java'], " \
                                         "excludes=INTERFACE_FILES)"
                    buck_file.write(
                        BUCK_FILE_TEMPLATE.format(
                            library_type=main_library_type,
                            sources=main_rule_srcs,
                            name=path.basename(root),
                            deps='\n'.join(
                                format_deps_for_buck_file(
                                    main_rule_deps))
                        ))
                    buck_files.append(main_buck_rule)

    return buck_files


def get_maven_coordinates(gradle_files, gradle_cache):
    maven_coordinates = {}
    for gradle_file in gradle_files:
        maven_coordinates.update(
            get_maven_coordinates_for_gradle_file(gradle_file, gradle_cache))
    return maven_coordinates


def get_maven_coordinates_for_gradle_file(gradle_file_path, gradle_cache):
    maven_coordinates = {}
    with open(gradle_file_path, 'r') as gradle_file:
        for line in gradle_file.readlines():
            match = THIRD_PARTY_JAR.match(line)
            if match:
                coordinate_match = MAVEN_COORDINATE.match(match.group(1))
                if coordinate_match:
                    prebuilt_type = 'prebuilt_jar'
                    binary_field = 'binary_jar'
                    group = coordinate_match.group(1)
                    dep_id = coordinate_match.group(2)
                    repo = 'mvn'
                    local_maven_repository = None

                    if coordinate_match.group(3):
                        dep_type = coordinate_match.group(3).rstrip(':')
                    else:
                        if group.startswith('com.google.android'):
                            local_maven_repository = path.join(
                                path.expandvars('$ANDROID_HOME'),
                                'extras/google/m2repository/')
                        elif group.startswith('com.android'):
                            local_maven_repository = path.join(
                                path.expandvars('$ANDROID_HOME'),
                                'extras/android/m2repository/')
                        else:
                            dep_type = 'jar'

                    version = coordinate_match.group(4)

                    dep_hash = None
                    if local_maven_repository:
                        maven_path = path.join(
                            local_maven_repository,
                            group.replace('.', '/'),
                            dep_id,
                            version
                        )

                        for possible_type in POSSIBLE_MAVEN_TYPES:
                            maven_sha = path.join(maven_path,
                                                  '{dep_id}-{version}.{type}'
                                                  '.sha1'
                                                  .format(
                                                      dep_id=dep_id,
                                                      version=version,
                                                      type=possible_type[0],
                                                  ))
                            if path.exists(maven_sha):
                                with open(maven_sha, 'r') as maven_sha_file:
                                    dep_type = possible_type[0]
                                    prebuilt_type = possible_type[1]
                                    binary_field = possible_type[2]
                                    dep_hash = maven_sha_file.read()
                    else:
                        for possible_type in POSSIBLE_MAVEN_TYPES:
                            expected_file = path.join(
                                '{dep_id}-{version}.{type}'
                                .format(
                                    dep_id=dep_id,
                                    version=version,
                                    type=possible_type[0],
                                ))
                            walker = os.walk(gradle_cache)
                            for root, dirs, files in walker:
                                if dep_hash:
                                    del dirs[:]
                                for child_file in files:
                                    if child_file == expected_file:
                                        dep_hash = path.basename(root)
                                        dep_type = possible_type[0]
                                        prebuilt_type = possible_type[1]
                                        binary_field = possible_type[2]
                                        del dirs[:]
                    if not dep_hash:
                        print "\tCoudn't find a hash for {0}".format(
                            coordinate_match.group(0))
                    else:
                        if len(dep_hash) % 2 != 0:
                            dep_hash = '0' + dep_hash
                        coordinate = "{group}:{id}:{type}:{version}".format(
                            group=group,
                            id=dep_id,
                            type=dep_type,
                            version=version,
                        )
                        maven_coordinates[coordinate] = {
                            'name': dep_id,
                            'repo': repo,
                            'prebuilt_type': prebuilt_type,
                            'binary_field': binary_field,
                            'coordinate': coordinate,
                            'hash': dep_hash
                        }

                else:
                    print "Couldn't parse maven coordiante {0}".format(
                        match.group(1))
    return maven_coordinates


def write_remote_deps(third_party_buck_file, maven_coordinates):
    existing_deps = get_existing_third_party_jars()
    if not os.path.exists(os.path.dirname(third_party_buck_file)):
        os.makedirs(os.path.dirname(third_party_buck_file))
    with open(third_party_buck_file, 'wa') as buck_file:
        for maven_coordinate in maven_coordinates.values():
            if maven_coordinate['name'] not in existing_deps:
                buck_file.write(REMOTE_DEP_TEMPLATE.format(**maven_coordinate))


def get_classes_for_aar(aar):
    temp_dir = tempfile.mkdtemp()
    try:
        with zipfile.ZipFile(aar) as aar_file:
            try:
                aar_file.extract('classes.jar', temp_dir)
                return get_classes_for_jar(path.join(temp_dir, 'classes.jar'))
            except KeyError:
                pass
    finally:
        shutil.rmtree(temp_dir)
    return []


def get_classes_for_jar(jar):
    jar_output = subprocess.check_output(['jar', 'tvf', jar])
    classes = []
    for line in jar_output.splitlines():
        match = CLASS_FILE.search(line)
        if match:
            classes.append(match.group(1).replace('/', '.').replace('$', '.'))
    return classes


def get_existing_third_party_jars():
    all_jar_targets = subprocess.check_output(['buck',
                                               'targets',
                                               '--type',
                                               'prebuilt_jar',
                                               'android_prebuilt_aar'],
                                              stderr=FNULL)
    result = set()
    for jar_target in all_jar_targets.splitlines():
        result.add(jar_target.rstrip().split(':')[1])
    return result


def create_third_party_map():
    third_party_map = {}
    android_libraries = set()
    all_jar_targets = subprocess.check_output(['buck',
                                               'targets',
                                               '--type',
                                               'prebuilt_jar'],
                                              stderr=FNULL)
    for jar_target in all_jar_targets.splitlines():
        subprocess.check_call(['buck',
                               'build',
                               jar_target],
                              stderr=FNULL)
        jar_location = subprocess.check_output(['buck',
                                                'targets',
                                                '--show_output',
                                                jar_target],
                                               stderr=FNULL).split(' ')[1]
        jar_location = jar_location.strip()
        for java_class in get_classes_for_jar(jar_location):
            third_party_map[java_class] = jar_target

    all_aar_targets = subprocess.check_output(['buck',
                                               'targets',
                                               '--type',
                                               'android_prebuilt_aar'],
                                              stderr=FNULL)
    for aar_target in all_aar_targets.splitlines():
        subprocess.check_call(['buck',
                               'build',
                               aar_target],
                              stderr=FNULL)
        aar_location = subprocess.check_output(['buck',
                                                'targets',
                                                '--show_output',
                                                aar_target],
                                               stderr=FNULL).split(' ')[1]
        aar_location = aar_location.strip()
        for java_class in get_classes_for_aar(aar_location):
            third_party_map[java_class] = aar_target
        android_libraries.add(aar_target)

    build_config_targets = subprocess.check_output(['buck',
                                                    'targets',
                                                    '--type',
                                                    'android_build_config'],
                                                   stderr=FNULL)
    for build_config_target in build_config_targets.splitlines():
        buck_file = build_config_target.split(':')[0].lstrip('/') + '/BUCK'
        with open(buck_file, 'r') as buck_file_contents:
            for line in buck_file_contents.readlines():
                line = line.rstrip()
                match = PACKAGE_DECLARATION.match(line)
                if match:
                    third_party_map[match.group(1) + '.BuildConfig'] = \
                        build_config_target
        android_libraries.add(build_config_target)

    android_resouce_targets = subprocess.check_output(['buck',
                                                       'targets',
                                                       '--type',
                                                       'android_resource'],
                                                      stderr=FNULL)

    for android_resouce_target in android_resouce_targets.splitlines():
        buck_file = android_resouce_target.split(':')[0].lstrip('/') + '/BUCK'
        with open(buck_file, 'r') as buck_file_contents:
            for line in buck_file_contents.readlines():
                line = line.rstrip()
                match = PACKAGE_DECLARATION.match(line)
                if match:
                    third_party_map[match.group(1) + '.R'] = \
                        android_resouce_target
        android_libraries.add(android_resouce_target)

    return third_party_map, android_libraries


def find_missing_deps_from_output(buck_rule, output):
    in_try_adding = False
    in_missing_deps = False
    missing_deps = set()
    for line in (x.strip() for x in output.splitlines()):
        if line == 'Try adding the following deps:':
            in_try_adding = True
        elif in_try_adding:
            if not line:
                in_try_adding = False
            else:
                missing_deps.add(line)
        elif line.endswith(' is missing deps:'):
            in_missing_deps = True
        elif in_missing_deps:
            match = DEP_DECLARATION.match(line)
            if match:
                missing_deps.add(match.group(1))
            else:
                in_missing_deps = False
    return {dep for dep in missing_deps if dep != buck_rule}


def add_missing_deps(buck_rules, android_libraries):
    settled = False
    pass_count = 1
    while not settled:
        print '\t*** Adding Deps: Pass {0}'.format(pass_count)
        files_changed = add_missing_deps_pass(buck_rules, android_libraries)
        print '\t*** Modified {0} BUCK files'.format(files_changed)
        settled = files_changed == 0
        pass_count += 1


def modify_buck_rule(buck_rule, new_deps_fn=None, new_rule_type=None):
    existing_deps = set()
    buck_file_with_new_deps = []
    found_deps_open = False
    found_deps_close = False
    found_rule_name = False
    buck_file = path.join(buck_rule.lstrip('/').split(':')[0], 'BUCK')
    rule_name = buck_rule.split(':')[1]
    modified_file = False
    with open(buck_file, 'r') as buck_file_contents:
        for line in buck_file_contents.readlines():
            line = line.rstrip()
            name_match = NAME_DECLARATION.match(line)
            if name_match and name_match.group(1) == rule_name:
                found_rule_name = True
                if (new_rule_type and
                        not buck_file_with_new_deps[-1].startswith(
                            new_rule_type)):
                    buck_file_with_new_deps[-1] = '{0}('.format(new_rule_type)
                    modified_file = True
                buck_file_with_new_deps.append(line)
            elif (found_rule_name and
                      DEPS_START.match(line) and
                      not found_deps_close):
                found_deps_open = True
            elif found_deps_open and not found_deps_close:
                if line.endswith('],'):
                    buck_file_with_new_deps.append('  deps = [')
                    new_deps = new_deps_fn(existing_deps)
                    buck_file_with_new_deps.extend(
                        format_deps_for_buck_file(new_deps))
                    buck_file_with_new_deps.append('  ],')
                    if new_deps != existing_deps:
                        modified_file = True
                    found_deps_close = True
                else:
                    match = DEP_DECLARATION.match(line)
                    if match:
                        existing_deps.add(match.group(1))
            else:
                buck_file_with_new_deps.append(line)
    if modified_file:
        import pdb;
        pdb.set_trace()
        with open(buck_file, 'w') as buck_file_contents:
            buck_file_contents.write(
                '\n'.join(buck_file_with_new_deps))

    return modified_file


def add_missing_deps_pass(buck_rules, android_libraries):
    files_changed = 0
    for rule in buck_rules:
        buck = subprocess.Popen(['buck', 'build', rule],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        _, err = buck.communicate()
        if buck.returncode != 0:
            missing_deps = find_missing_deps_from_output(rule, err)
            new_rule_type = None
            if rule in android_libraries:
                new_rule_type = 'android_library'
            existing_deps = set()

            def update_deps(x):
                existing_deps.update(x)
                return x.union(missing_deps)

            if modify_buck_rule(rule,
                                new_deps_fn=update_deps,
                                new_rule_type=new_rule_type):
                files_changed += 1
            for dep in missing_deps.union(existing_deps):
                if dep in android_libraries:
                    android_libraries.add(rule)

    return files_changed


def get_files_for_rule(buck_rule):
    existing_deps = set()

    def empty_deps(x):
        existing_deps.update(x)
        return set()

    modify_buck_rule(buck_rule, new_deps_fn=empty_deps)
    files = subprocess.check_output(['buck',
                                     'audit',
                                     'input',
                                     buck_rule],
                                    stderr=FNULL).splitlines()
    modify_buck_rule(buck_rule, new_deps_fn=existing_deps.union)
    return files


def find_cycle():
    process = subprocess.Popen(['buck', 'targets'],
                               stdout=FNULL,
                               stderr=subprocess.PIPE)
    _, stderr = process.communicate()
    retcode = process.poll()
    if retcode:
        for line in stderr.splitlines():
            if line.startswith(CYCLE_PREFIX):
                return line[len(CYCLE_PREFIX):].split(' -> ')

    return []


def find_smallest_dep(cycle):
    small_dep = None
    result = ()
    for i in xrange(len(cycle)):
        current = cycle[i]
        next = cycle[(i + 1) % len(cycle)]
        current_files = set(get_files_for_rule(current))
        next_files = set(get_files_for_rule(current))
        import pdb;
        pdb.set_trace()


def break_cycle():
    cycle = find_cycle()
    if cycle:
        find_smallest_dep(cycle)


def create_parser():
    parser = argparse.ArgumentParser(
        description='Generate a skeleton buck project from a gradle project.')
    parser.add_argument(
        '--gradle_cache',
        dest='gradle_cache',
        help='Path to gradle cache',
        default=path.expandvars(path.join('$HOME', '.gradle', 'caches')),
    )
    parser.add_argument(
        '--third_party_buck',
        dest='third_party_buck',
        help='Path to third party code buck file',
        default='libs/BUCK'
    )
    parser.add_argument(
        '--split_interfaces',
        dest='split_interfaces',
        help='Whether or not to split interfaces into their own rule.',
        action='store_true',
        default=False
    )

    return parser


def main():
    print "**** Creating remote_file rules for maven deps ***"

    gradle_files = []
    src_roots = []
    android_directories = []
    external_maven_repos = set()
    for root, dirs, files in os.walk(os.getcwd(), followlinks=True):
        if 'build.gradle' in files:
            gradle_file = path.join(root, 'build.gradle')
            gradle_files.append(gradle_file)
            external_maven_repos = external_maven_repos.union(
                get_repositories_from_gradle_file(gradle_file))
            main_root = path.join(root, 'src', 'main')
            java_root = path.join(main_root, 'java')
            if path.exists(java_root):
                src_roots.append(path.relpath(java_root))
            if path.exists(path.join(main_root, 'AndroidManifest.xml')):
                android_directories.append(main_root)

    if not gradle_files:
        raise Exception("Couldn't find any 'build.gradle' files.")

    if not path.exists('.buckconfig'):
        maven_repos = ['mvn{0} = {1}'.format(i, repo)
                       for i, repo
                       in enumerate(external_maven_repos)]
        with open('.buckconfig', 'w') as buck_config:
            buck_config.write(BUCK_CONFIG_TEMPLATE.format(
                src_roots=','.join(['/' + x for x in src_roots]),
                maven_repositories='  \n'.join(maven_repos)))

    for android_directory in android_directories:
        buck_file = path.join(android_directory, 'BUCK')
        if path.exists(buck_file):
            continue
        with open(path.join(android_directory,
                            'AndroidManifest.xml'), 'r') as manifest_file:
            manifest_xml = xml.parse(manifest_file)
            package = manifest_xml.getroot().get('package')
            with open(buck_file, 'w') as buck_file_handle:
                buck_file_handle.write(ANDROID_BUILD_CONFIG_TEMPLATE.format(
                    package=package
                ))
                if path.exists(path.join(android_directory, 'res')):
                    buck_file_handle.write(ANDROID_RESOURCE_TEMPLATE.format(
                        package=package
                    ))

    maven_coordinates = get_maven_coordinates(gradle_files,
                                              args.gradle_cache)
    write_remote_deps(args.third_party_buck, maven_coordinates)

    third_party_map, android_libraries = create_third_party_map()

    src_roots = get_source_roots('.buckconfig')

    print "**** Generating Buck Files ***"
    buck_rules = generate_default_buck_files(
        '.buckconfig',
        src_roots,
        third_party_map,
        android_libraries,
        'java_library')

    print "**** Adding missing dependencies ***"
    add_missing_deps(buck_rules, android_libraries)

    print "**** Checking which rules compile ***"
    passing_count = 0

    for buck_rule in buck_rules:
        try:
            subprocess.check_call(['buck', 'build', path.relpath(buck_rule)],
                                  stdout=FNULL,
                                  stderr=FNULL)
            passing_count += 1
        except:
            pass

    print '{0} out of {1} rules compile!!!'.format(passing_count,
                                                   len(buck_rules))


if __name__ == '__main__':
    args = create_parser().parse_args()
    main()
