# gradle_to_buck

Scripts to help migrate existing Gradle projects to Buck

This python script will take an existing Gradle project and generate a BUCK file per
Java package, generating all dependencies for you.

# WARNING

This is a script I am hacking on and is likely not fit for external use.

# Usage
First, you should build your project with Gradle to fetch all dependencies needed.

Optionally, use Android Studio's `Optimize Imports` feature to remove any unneeded
dependencies between Java packages.

Buck encourages the creation of many small modules, ideally at the Java Package level.
Buck, however, doesn't allow cyclic dependencies between modules.  Thankfully, 
IntelliJ provides tools to tease these apart.

https://www.jetbrains.com/idea/help/analyzing-cyclic-dependencies.html

Next, run 
`python $PATH_TO_THIS_REPO/buck_file_generator.py`

And all of your Java modules will have a BUCK file generated for them.
