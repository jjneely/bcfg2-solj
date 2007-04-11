'''Bcfg2 Support for RPMS'''

__revision__ = '$Revision$'

import Bcfg2.Client.Tools, rpmtools, os.path, rpm, ConfigParser


class RPMng(Bcfg2.Client.Tools.PkgTool):
    '''Support for RPM packages'''
    __name__ = 'RPMng'
    __execs__ = ['/bin/rpm', '/var/lib/rpm']
    __handles__ = [('Package', 'rpm')]

    __req__ = {'Package': ['name', 'version']}
    __ireq__ = {'Package': ['name', 'version', 'url']}
    
    __new_req__ = {'Package': ['name'], 'Instance': ['version', 'release', 'arch']}
    __new_ireq__ = {'Package': ['name', 'uri'], \
                    'Instance': ['simplefile', 'version', 'release', 'arch']}
    
    __gpg_req__ = {'Package': ['name', 'version']}
    __gpg_ireq__ = {'Package': ['name', 'version']}
    
    __new_gpg_req__ = {'Package': ['name'], 'Instance': ['version', 'release']}
    __new_gpg_ireq__ = {'Package': ['name'], 'Instance': ['version', 'release']}

    conflicts = ['RPM']

    pkgtype = 'rpm'
    pkgtool = ("rpm --oldpackage --replacepkgs --quiet -U %s", ("%s", ["url"]))

    
    def __init__(self, logger, setup, config, states):
        Bcfg2.Client.Tools.PkgTool.__init__(self, logger, setup, config, states)

        self.installOnlyPkgs = []
        self.erase_flags = []
        self.instance_status = {}
        self.extra_instances = []
        self.gpg_keyids = self.getinstalledgpg()

        # Process thee RPMng section from the config file.
        RPMng_CP = ConfigParser.ConfigParser()
        RPMng_CP.read(self.setup.get('setup'))

        # installonlypackages
        if RPMng_CP.has_option('RPMng','installonlypackages'):
            self.installOnlyPkgs = RPMng_CP.get('RPMng','installonlypackages').split(',') + \
                                                                                   ['gpg-pubkey']
        if self.installOnlyPkgs == []:
            self.installOnlyPkgs = ['kernel', 'kernel-bigmem', 'kernel-enterprise', 'kernel-smp',
                               'kernel-modules', 'kernel-debug', 'kernel-unsupported',
                               'kernel-source', 'kernel-devel', 'kernel-default',
                               'kernel-largesmp-devel', 'kernel-largesmp', 'kernel-xen', 
                               'gpg-pubkey']
        self.logger.debug('installOnlyPackages = %s' % self.installOnlyPkgs)

        # erase_flags
        if RPMng_CP.has_option('RPMng','erase_flags'):
            self.installOnlyPkgs = RPMng_CP.get('RPMng','erase_flags').split(',')
        if self.erase_flags == []:
            self.erase_flags = ['allmatches']
        self.logger.debug('erase_flags = %s' % self.erase_flags)

    def RefreshPackages(self):
        '''
            Creates self.installed{} which is a dict of installed packages.

            The dict items are lists of nevra dicts.  This loosely matches the
            config from the server and what rpmtools uses to specify pacakges.

            e.g.

            self.installed['foo'] = [ {'name':'foo', 'epoch':None, 
                                       'version':'1', 'release':2, 
                                       'arch':'i386'},
                                      {'name':'foo', 'epoch':None, 
                                       'version':'1', 'release':2, 
                                       'arch':'x86_64'} ]
        '''
        self.installed = {}
        refresh_ts = rpmtools.rpmtransactionset()
        # Don't bother with signature checks at this stage. The GPG keys might 
        # not be installed.
        refresh_ts.setVSFlags(rpm._RPMVSF_NODIGESTS|rpm._RPMVSF_NOSIGNATURES)
        for nevra in rpmtools.rpmpackagelist(refresh_ts):
            self.installed.setdefault(nevra['name'], []).append(nevra)
        if self.setup['debug']:
            print "The following package instances are installed:"
            for name, instances in self.installed.iteritems():
                self.logger.info("    " + name)
                for inst in instances:
                    self.logger.info("        %s" %self.str_evra(inst)) 
        refresh_ts.closeDB()
        del refresh_ts

    def VerifyPackage(self, entry, modlist):
        '''
            Verify Package status for entry.
            Performs the following:
                - Checks for the presence of required Package Instances.
                - Compares the evra 'version' info against self.installed{}.
                - RPM level package verify (rpm --verify).
                - Checks for the presence of unrequired package instances.

            Produces the following dict and list for RPMng.Install() to use:
              For installs/upgrades/fixes of required instances:
                instance_status = { <Instance Element Object>:
                                       { 'installed': True|False,
                                         'version_fail': True|False,
                                         'verify_fail': True|False,
                                         'pkg': <Package Element Object>,
                                         'modlist': [ <filename>, ... ],
                                         'verify' : [ <rpm --verify results> ]  
                                       }, ......
                                  }

              For deletions of unrequired instances:
                extra_instances = [ <Package Element Object>, ..... ]

              Constructs the text prompts for interactive mode.
        '''
        if len(entry) == 0:
            # We have an old style no Instance entry. Convert it to new style.
            version, release = entry.get('version').split('-')
            instance = Bcfg2.Client.XML.SubElement(entry, 'Package')
            for attrib in entry.attrib.keys():
                instance.attrib[attrib] = entry.attrib[attrib]
            instance.set('version', version)
            instance.set('release', release)
            instances = [ instance ]
        else:
            # We have a new style entry or a previously converted old style entry.
            instances = [inst for inst in entry if inst.tag == 'Instance' or inst.tag == 'Package']

        self.logger.info("Verifying package instances for %s" % entry.get('name'))
        package_fail = False
        qtext_versions = ''

        if self.installed.has_key(entry.get('name')):
            # There is at least one instance installed.
            if entry.get('name') in self.installOnlyPkgs:
                # Packages that should only be installed or removed.
                # e.g. kernels.
                self.logger.info("        Install only package.")
                for inst in instances:
                    self.instance_status.setdefault(inst, {})['installed'] = False
                    self.instance_status[inst]['version_fail'] = False
                    if inst.tag == 'Package' and len(self.installed[entry.get('name')]) > 1:
                        self.logger.error("WARNING: Multiple instances of package %s are installed." % \
                                                                               (entry.get('name')))
                    for pkg in self.installed[entry.get('name')]:
                        if self.pkg_vr_equal(inst, pkg) or self.inst_evra_equal(inst, pkg):
                            self.logger.info("        %s" % self.str_evra(inst))
                            self.logger.debug("        verify_flags = %s" % \
                                                           (inst.get('verify_flags', [])))
                            self.instance_status[inst]['installed'] = True
     
                            flags = inst.get('verify_flags', '').split(',')
                            if pkg.get('gpgkeyid', '')[-8:] not in self.gpg_keyids and \
                               entry.get('name') != 'gpg-pubkey':
                                flags += ['nosignature', 'nodigest']
                                self.logger.info('WARNING: Package %s %s requires GPG Public key with ID %s'\
                                                   % (pkg.get('name'), self.str_evra(pkg), \
                                                      pkg.get('gpgkeyid', '')))
                                self.logger.info('         Disabling signature check.')

                            self.instance_status[inst]['verify'] = \
                                                      rpmtools.rpm_verify( self.vp_ts, pkg, flags)

                    if self.instance_status[inst]['installed'] == False:
                        self.logger.info("        Package %s %s not installed." % \
                                     (entry.get('name'), self.str_evra(inst)))
                            
                        qtext_versions = qtext_versions + 'I(%s) ' % self.str_evra(inst)
                        entry.set('current_exists', 'false')
            else:
                # Normal Packages that can be upgraded.
                for inst in instances:
                    self.instance_status.setdefault(inst, {})['installed'] = False
                    self.instance_status[inst]['version_fail'] = False

                    # Only installed packages with the same architecture are
                    # relevant.
                    if inst.get('arch', None) == None:
                        arch_match = self.installed[entry.get('name')]
                    else:
                        arch_match = [pkg for pkg in self.installed[entry.get('name')] \
                                          if pkg.get('arch', None) == inst.get('arch', None)]

                    if len(arch_match) > 1:
                        self.logger.error("Multiple instances of package %s installed with the same achitecture." % \
                                              (entry.get('name')))
                    elif len(arch_match) == 1:
                        # There is only one installed like there should be.
                        # Check that it is the right version.
                        for pkg in arch_match:
                            if self.pkg_vr_equal(inst, pkg) or self.inst_evra_equal(inst, pkg):
                                self.logger.info("        %s" % self.str_evra(inst))
                                self.logger.debug("        verify_flags = %s" % \
                                                              (inst.get('verify_flags', [])))
                                self.instance_status[inst]['installed'] = True

                                flags = inst.get('verify_flags', '').split(',') 
                                if pkg.get('gpgkeyid', '')[-8:] not in self.gpg_keyids:
                                    flags += ['nosignature', 'nodigest']
                                    self.logger.info('WARNING: Package %s %s requires GPG Public key with ID %s'\
                                                       % (pkg.get('name'), self.str_evra(pkg), \
                                                          pkg.get('gpgkeyid', '')))
                                    self.logger.info('         Disabling signature check.')

                                self.instance_status[inst]['verify'] = \
                                                      rpmtools.rpm_verify( self.vp_ts, pkg, flags )

                            else:
                                # Wrong version installed.
                                self.instance_status[inst]['version_fail'] = True
                                self.logger.info("        Wrong version installed.  Want %s, but have %s"\
                                                       % (self.str_evra(inst), self.str_evra(pkg)))
                        
                                qtext_versions = qtext_versions + 'U(%s -> %s) ' % \
                                                          (self.str_evra(pkg), self.str_evra(inst))
                    elif len(arch_match) == 0:
                        # This instance is not installed.
                        self.instance_status[inst]['installed'] = False
                        self.logger.info("        %s is not installed." % self.str_evra(inst))
                        qtext_versions = qtext_versions + 'I(%s) ' % self.str_evra(inst)

            # Check the rpm verify results.
            for inst in instances:
                instance_fail = False
                # Dump the rpm verify results. 
                #****Write something to format this nicely.*****
                if self.setup['debug'] and self.instance_status[inst].get('verify', None):
                    self.logger.debug(self.instance_status[inst]['verify'])

                self.instance_status[inst]['verify_fail'] = False
                if self.instance_status[inst].get('verify', None):
                    if len(self.instance_status[inst].get('verify')) > 1:
                        self.logger.info("WARNING: Verification of more than one package instance.")
                 

                    for result in self.instance_status[inst]['verify']:

                        # Check header results
                        if result.get('hdr', None):
                            instance_fail = True
                            self.instance_status[inst]['verify_fail'] = True
    
                        # Check dependency results
                        if result.get('deps', None):
                            instance_fail = True
                            self.instance_status[inst]['verify_fail'] = True
                         
                        # Check the rpm verify file results against the modlist
                        # and per Instance Ignores.
                        for file_result in result.get('files', []):
                            if file_result[-1] not in modlist and \
                               file_result[-1] not in \
                                          [ignore.get('name') for ignore in inst.findall('Ignore')]:
                                instance_fail = True
                                self.instance_status[inst]['verify_fail'] = True
                            else:
                                self.logger.info("        Modlist/Ignore match: %s" % \
                                                                               (file_result[-1]))

                    if instance_fail == True:
                        self.logger.info("*** Instance %s failed RPM verification ***" % \
                                                                               self.str_evra(inst))
                        qtext_versions = qtext_versions + 'R(%s) ' % self.str_evra(inst)
                        self.instance_status[inst]['modlist'] = modlist

                if self.instance_status[inst]['installed'] == False or \
                   self.instance_status[inst].get('version_fail', False)== True or \
                   self.instance_status[inst].get('verify_fail', False) == True:
                    package_fail = True
                    self.instance_status[inst]['pkg'] = entry
                    self.instance_status[inst]['modlist'] = modlist

            # Find Installed Instances that are not in the Config.
            extra_installed = self.FindExtraInstances(entry, self.installed[entry.get('name')])
            if extra_installed != None:
                package_fail = True
                self.extra_instances.append(extra_installed)
                for inst in extra_installed.findall('Instance'):
                    qtext_versions = qtext_versions + 'D(%s) ' % self.str_evra(inst)
                self.logger.debug("Found Extra Instances %s" % qtext_versions)
           
            if package_fail == True:
                self.logger.info("        Package %s failed verification." % (entry.get('name')))
                qtext = 'Install/Upgrade/delete Package %s instance(s) - %s (y/N) ' % \
                                              (entry.get('name'), qtext_versions)
                entry.set('qtext', qtext)

                bcfg2_versions = ''
                for bcfg2_inst in [inst for inst in instances if inst.tag == 'Instance']:
                    bcfg2_versions = bcfg2_versions + '(%s) ' % self.str_evra(bcfg2_inst)
                if bcfg2_versions != '':
                    entry.set('version', bcfg2_versions)
                installed_versions = ''

                for installed_inst in self.installed[entry.get('name')]:
                    installed_versions = installed_versions + '(%s) ' % \
                                                                    self.str_evra(installed_inst)

                entry.set('current_version', installed_versions)
                return False

        else:
            # There are no Instances of this package installed.
            self.logger.debug("Package %s has no instances installed" % (entry.get('name')))
            entry.set('current_exists', 'false')
            bcfg2_versions = ''
            for inst in instances:
                qtext_versions = qtext_versions + 'I(%s) ' % self.str_evra(inst)
                self.instance_status.setdefault(inst, {})['installed'] = False
                self.instance_status[inst]['modlist'] = modlist
                self.instance_status[inst]['pkg'] = entry
                if inst.tag == 'Instance':
                    bcfg2_versions = bcfg2_versions + '(%s) ' % self.str_evra(inst)
            if bcfg2_versions != '':
                entry.set('version', bcfg2_versions)
            entry.set('qtext', "Install Package %s Instance(s) %s? (y/N) " % \
                                                        (entry.get('name'), qtext_versions))

            return False
        return True

    def RemovePackages(self, packages):
        '''
           Remove specified entries.

           packages is a list of Package Entries with Instances generated 
           by FindExtraPackages().
        '''
        self.logger.debug('Running RPMng.RemovePackages()')

        pkgspec_list = []
        for pkg in packages:
            for inst in pkg:
                if pkg.get('name') != 'gpg-pubkey':
                    pkgspec = { 'name':pkg.get('name'),
                            'epoch':inst.get('epoch', None),
                            'version':inst.get('version'),
                            'release':inst.get('release'),
                            'arch':inst.get('arch') }
                    pkgspec_list.append(pkgspec)
                else:
                    pkgspec = { 'name':pkg.get('name'),
                            'version':inst.get('version'),
                            'release':inst.get('release')}
                    self.logger.info("WARNING: gpg-pubkey package not in configuration %s %s"\
                                                 % (pkgspec.get('name'), self.str_evra(pkgspec)))
                    self.logger.info("         This package will be deleted in a future version of the RPMng driver.")
                #pkgspec_list.append(pkg_spec)

        erase_results = rpmtools.rpm_erase(pkgspec_list, self.erase_flags) 
        if erase_results == []:
            self.modified += packages
            for pkg in pkgspec_list:
                self.logger.info("Deleted %s %s" % (pkg.get('name'), self.str_evra(pkg)))
        else:
            self.logger.info("Bulk erase failed with errors:")
            self.logger.debug("Erase results = %s" % erase_results)
            self.logger.info("Attempting individual erase for each package.")
            pkgspec_list = []
            for pkg in packages:
                pkg_modified = False
                for inst in pkg:
                    if pkg.get('name') != 'gpg-pubkey':
                        pkgspec = { 'name':pkg.get('name'),
                                'epoch':inst.get('epoch', None),
                                'version':inst.get('version'),
                                'release':inst.get('release'),
                                'arch':inst.get('arch') }
                        pkgspec_list.append(pkgspec)
                    else:
                        pkgspec = { 'name':pkg.get('name'),
                                'version':inst.get('version'),
                                'release':inst.get('release')}
                        self.logger.info("WARNING: gpg-pubkey package not in configuration %s %s"\
                                                   % (pkgspec.get('name'), self.str_evra(pkgspec)))
                        self.logger.info("         This package will be deleted in a future version of the RPMng driver.")
                        continue # Don't delete the gpg-pubkey packages for now.
                    erase_results = rpmtools.rpm_erase([pkgspec], self.erase_flags) 
                    if erase_results == []:
                        pkg_modified = True
                        self.logger.info("Deleted %s %s" % \
                                                   (pkgspec.get('name'), self.str_evra(pkgspec)))
                    else:
                        self.logger.error("unable to delete %s %s" % \
                                                   (pkgspec.get('name'), self.str_evra(pkgspec)))
                        self.logger.debug("Failure = %s" % erase_results)
                if pkg_modified == True:
                    self.modified.append(pkg)

        self.RefreshPackages()
        self.extra = self.FindExtraPackages()

    def reinstall_check(self, verify_results):
        '''
           Control if a reinstall of a package happens or not based on the 
           results from RPMng.VerifyPackage().

           Return True to reinstall, False to not reintstall.
        '''
        reinstall = False

        for inst in verify_results.get('verify'):
            self.logger.debug('reinstall_check: %s %s:%s-%s.%s' % inst.get('nevra'))

            # Parse file results
            for file_result in inst.get('files'):
                self.logger.debug('reinstall_check: file: %s' % file_result)
                if file_result[-2] != 'c':
                    reinstall = True

        return reinstall
    
    def Install(self, packages):
        '''
           Try and fix everything that RPMng.VerifyPackages() found wrong for 
           each Package Entry.  This can result in individual RPMs being 
           installed (for the first time), reinstalled, deleted, downgraded 
           or upgraded.

           packages is a list of Package Elements that has 
               self.states[<Package Element>] == False

           The following effects occur:
           - self.states{} is conditionally updated for each package.
           - self.installed{} is rebuilt, possibly multiple times.
           - self.instance_statusi{} is conditionally updated for each instance 
             of a package.
           - Each package will be added to self.modified[] if its self.states{} 
             entry is set to True. 
        '''
        self.logger.info('Runing RPMng.Install()')

        install_only_pkgs = []
        gpg_keys = []
        upgrade_pkgs = []

        # Remove extra instances.
        # Can not reverify because we don't have a package entry.
        if len(self.extra_instances) > 0:
            self.RemovePackages(self.extra_instances)

        # Figure out which instances of the packages actually need something
        # doing to them and place in the appropriate work 'queue'.
        for pkg in packages:
            for inst in [inst for inst in pkg if inst.tag == 'Instance' or inst.tag == 'Package']:
                if self.instance_status[inst].get('installed', False) == False or \
                   self.instance_status[inst].get('version_fail', False) == True or \
                   (self.instance_status[inst].get('verify_fail', False) == True and \
                    self.reinstall_check(self.instance_status[inst])):
                    if pkg.get('name') == 'gpg-pubkey':
                        gpg_keys.append(inst)
                    elif pkg.get('name') in self.installOnlyPkgs:
                        install_only_pkgs.append(inst)
                    else:
                        upgrade_pkgs.append(inst)

        # Fix installOnlyPackages
        if len(install_only_pkgs) > 0:
            self.logger.info("Attempting to install 'install only packages'")
            install_args = " ".join([os.path.join(self.instance_status[inst].get('pkg').get('uri'), \
                                                  inst.get('simplefile')) \
                                           for inst in install_only_pkgs])
            self.logger.debug("rpm --install --quiet --oldpackage %s" % install_args)
            cmdrc, output = self.cmd.run("rpm --install --quiet --oldpackage --replacepkgs %s" % \
                                                                                     install_args)
            if cmdrc == 0:
                # The rpm command succeeded.  All packages installed.
                self.logger.info("Single Pass for InstallOnlyPkgs Succeded")
                self.RefreshPackages()

                # Reverify all the packages that we might have just changed.
                # There may be multiple instances per package, only do the 
                # verification once.
                install_pkg_set = set([self.instance_status[inst].get('pkg') \
                                                      for inst in install_only_pkgs])
                self.logger.info("Reverifying InstallOnlyPkgs")
                for inst in install_only_pkgs:
                    pkg_entry = self.instance_status[inst].get('pkg')
                    if pkg_entry in install_pkg_set:
                        self.logger.debug("Reverifying InstallOnlyPkg %s" % \
                                                                      (pkg_entry.get('name')))
                        install_pkg_set.remove(pkg_entry)
                        self.states[pkg_entry] = self.VerifyPackage(pkg_entry, \
                                                         self.instance_status[inst].get('modlist'))
                    else:
                        # We already reverified this pacakge.
                        continue
            else:
                # The rpm command failed.  No packages installed.
                # Try installing instances individually.
                self.logger.error("Single Pass for InstallOnlyPackages Failed")
                installed_instances = []
                for inst in install_only_pkgs:
                    install_args = os.path.join(self.instance_status[inst].get('pkg').get('uri'), \
                                                     inst.get('simplefile'))
                    self.logger.debug("rpm --install --quiet --oldpackage %s" % install_args)
                    cmdrc, output = self.cmd.run("rpm --install --quiet --oldpackage --replacepkgs %s" % \
                                                                                      install_args)
                    if cmdrc == 0:
                        installed_instances.append(inst)
                    else:
                        self.logger.debug("InstallOnlyPackage %s %s would not install." % \
                                              (self.instance_status[inst].get('pkg').get('name'),\
                                               self.str_evra(inst)))

                install_pkg_set = set([self.instance_status[inst].get('pkg') \
                                                      for inst in install_only_pkgs])
                self.RefreshPackages()
                for inst in installed_instances:
                    pkg = inst.get('pkg')
                    # Reverify all the packages that we might have just changed.
                    # There may be multiple instances per package, only do the
                    # verification once.
                    if pkg in install_pkg_set:
                        self.logger.debug("Reverifying InstallOnlyPkg %s" % \
                                                                      (pkg_entry.get('name')))
                        install_pkg_set.remove(pkg)
                        self.states[pkg_entry] = self.VerifyPackage(pkg, \
                                                         self.instance_status[inst].get('modlist'))
                    else:
                        # We already reverified this pacakge.
                        continue

        # Install GPG keys.
        if len(gpg_keys) > 0:
            for inst in gpg_keys:
                self.logger.info("Installing GPG keys.")
                key_arg = os.path.join(self.instance_status[inst].get('pkg').get('uri'), \
                                                     inst.get('simplefile'))
                cmdrc, output = self.cmd.run("rpm --import %s" % key_arg)
                if cmdrc != 0:
                    self.logger.debug("Unable to install %s-%s" % \
                                              (self.instance_status[inst].get('pkg').get('name'), \
                                               self.str_evra(inst)))
                else:
                    self.logger.debug("Installed %s-%s-%s" % \
                                              (self.instance_status[inst].get('pkg').get('name'), \
                                               inst.get('version'), inst.get('release')))
            self.RefreshPackages()
            self.gpg_keyids = self.getinstalledgpg()
            pkg = self.instance_status[gpg_keys[0]].get('pkg')
            self.states[pkg] = self.VerifyPackage(pkg, [])

        # Fix upgradeable packages.
        if len(upgrade_pkgs) > 0:
            self.logger.info("Attempting to upgrade packages")
            upgrade_args = " ".join([os.path.join(self.instance_status[inst].get('pkg').get('uri'), \
                                                  inst.get('simplefile')) \
                                           for inst in upgrade_pkgs])
            cmdrc, output = self.cmd.run("rpm --upgrade --quiet --oldpackage --replacepkgs %s" % \
                                                       upgrade_args)
            if cmdrc == 0:
                # The rpm command succeeded.  All packages upgraded.
                self.logger.info("Single Pass for Upgraded Packages Succeded")
                upgrade_pkg_set = set([self.instance_status[inst].get('pkg') \
                                                      for inst in upgrade_pkgs])
                self.RefreshPackages()
                for inst in upgrade_pkgs:
                    pkg_entry = self.instance_status[inst].get('pkg')
                    # Reverify all the packages that we might have just changed.
                    # There may be multiple instances per package, only do the 
                    # verification once.
                    if pkg_entry in upgrade_pkg_set:
                        self.logger.debug("Reverifying Upgradable Package %s" % \
                                                                      (pkg_entry.get('name')))
                        upgrade_pkg_set.remove(pkg_entry)
                        self.states[pkg_entry] = self.VerifyPackage(pkg_entry, 
                                                          self.instance_status[inst].get('modlist'))
                    else:
                        # We already reverified this pacakge.
                        continue
            else:
                # The rpm command failed.  No packages upgraded.
                # Try upgrading instances individually.
                self.logger.error("Single Pass for Upgrading Packages Failed")
                upgraded_instances = []
                for inst in upgrade_pkgs:
                    upgrade_args = os.path.join(self.instance_status[inst].get('pkg').get('uri'), \
                                                     inst.get('simplefile'))
                    #self.logger.debug("rpm --upgrade --quiet --oldpackage --replacepkgs %s" % \
                    #                                                      upgrade_args)
                    cmdrc, output = self.cmd.run("rpm --upgrade --quiet --oldpackage --replacepkgs %s" % upgrade_args)
                    if cmdrc == 0:
                        upgraded_instances.append(inst)
                    else:
                        self.logger.debug("Package %s %s would not upgrade." % \
                                              (self.instance_status[inst].get('pkg').get('name'),\
                                               self.str_evra(inst)))

                upgrade_pkg_set = set([self.instance_status[inst].get('pkg') \
                                                      for inst in upgrade_pkgs])
                self.RefreshPackages()
                for inst in upgraded_instances:
                    pkg_entry = self.instance_status[inst].get('pkg')
                    # Reverify all the packages that we might have just changed.
                    # There may be multiple instances per package, only do the
                    # verification once.
                    if pkg_entry in upgrade_pkg_set:
                        self.logger.debug("Reverifying Upgradable Package %s" % \
                                                                      (pkg_entry.get('name')))
                        upgrade_pkg_set.remove(pkg_entry)
                        self.states[pkg_entry] = self.VerifyPackage(pkg_entry, \
                                                        self.instance_status[inst].get('modlist'))
                    else:
                        # We already reverified this pacakge.
                        continue

        for entry in [ent for ent in packages if self.states[ent]]:
            self.modified.append(entry)

    def canInstall(self, entry):
        '''
            test if entry has enough information to be installed
        '''
        if not self.handlesEntry(entry):
            return False

        instances = entry.findall('Instance')

        if not instances:
            # Old non Instance format, unmodified.
            if entry.get('name') == 'gpg-pubkey':
                # gpg-pubkey packages aren't really pacakges, so we have to do
                # something a little different.
                # Check that the Package Level has what we need for verification.
                if [attr for attr in self.__gpg_ireq__[entry.tag] if attr not in entry.attrib]:
                    self.logger.error("Incomplete information for entry %s:%s; cannot install" \
                                      % (entry.tag, entry.get('name')))
                    return False
            else:
                if [attr for attr in self.__ireq__[entry.tag] if attr not in entry.attrib]:
                    self.logger.error("Incomplete information for entry %s:%s; cannot install" \
                                      % (entry.tag, entry.get('name')))
                    return False
        else:
            if entry.get('name') == 'gpg-pubkey':
                # gpg-pubkey packages aren't really pacakges, so we have to do
                # something a little different.
                # Check that the Package Level has what we need for verification.
                if [attr for attr in self.__new_gpg_ireq__[entry.tag] if attr not in entry.attrib]:
                    self.logger.error("Incomplete information for entry %s:%s; cannot install" \
                                      % (entry.tag, entry.get('name')))
                    return False
                # Check that the Instance Level has what we need for verification.
                for inst in instances:
                    if [attr for attr in self.__new_gpg_ireq__[inst.tag] \
                                 if attr not in inst.attrib]:
                        self.logger.error("Incomplete information for entry %s:%s; cannot install"\
                                          % (inst.tag, inst.get('name')))
                        return False
            else:
                # New format with Instances.
                # Check that the Package Level has what we need for verification.
                if [attr for attr in self.__new_ireq__[entry.tag] if attr not in entry.attrib]:
                    self.logger.error("Incomplete information for entry %s:%s; cannot install" \
                                      % (entry.tag, entry.get('name')))
                    return False
                # Check that the Instance Level has what we need for verification.
                for inst in instances:
                    if inst.tag == 'Instance':
                        if [attr for attr in self.__new_ireq__[inst.tag] \
                                     if attr not in inst.attrib]:
                            self.logger.error("Incomplete information for entry %s:%s; cannot install" \
                                              % (inst.tag, inst.get('name')))
                            return False
        return True

    def canVerify(self, entry):
        '''
            Test if entry has enough information to be verified.
 
            Three types of entries are checked.
               Old style Package
               New style Package with Instances
               pgp-pubkey packages

           Also the old style entries get modified after the first 
           VerifyPackage() run, so there needs to be a second test.
        '''
        if not self.handlesEntry(entry):
            return False

        instances = entry.findall('Instance')

        if not instances:
            # Old non Instance format, unmodified.
            if entry.get('name') == 'gpg-pubkey':
                # gpg-pubkey packages aren't really pacakges, so we have to do 
                # something a little different.
                # Check that the Package Level has what we need for verification.
                if [attr for attr in self.__gpg_req__[entry.tag] if attr not in entry.attrib]:
                    self.logger.error("Incomplete information for entry %s:%s; cannot verify" \
                                      % (entry.tag, entry.get('name')))
                    return False
            else:
                if [attr for attr in self.__req__[entry.tag] if attr not in entry.attrib]:
                    self.logger.error("Incomplete information for entry %s:%s; cannot verify" \
                                      % (entry.tag, entry.get('name')))
                    return False
        else:
            if entry.get('name') == 'gpg-pubkey':
                # gpg-pubkey packages aren't really pacakges, so we have to do 
                # something a little different.
                # Check that the Package Level has what we need for verification.
                if [attr for attr in self.__new_gpg_req__[entry.tag] if attr not in entry.attrib]:
                    self.logger.error("Incomplete information for entry %s:%s; cannot verify" \
                                      % (entry.tag, entry.get('name')))
                    return False
                # Check that the Instance Level has what we need for verification.
                for inst in instances:
                    if [attr for attr in self.__new_gpg_req__[inst.tag] \
                                 if attr not in inst.attrib]:
                        self.logger.error("Incomplete information for entry %s:%s; cannot verify" \
                                          % (inst.tag, inst.get('name')))
                        return False
            else:
                # New format with Instances, or old style modified.
                # Check that the Package Level has what we need for verification.
                if [attr for attr in self.__new_req__[entry.tag] if attr not in entry.attrib]:
                    self.logger.error("Incomplete information for entry %s:%s; cannot verify" \
                                      % (entry.tag, entry.get('name')))
                    return False
                # Check that the Instance Level has what we need for verification.
                for inst in instances:
                    if inst.tag == 'Instance':
                        if [attr for attr in self.__new_req__[inst.tag] \
                                     if attr not in inst.attrib]:
                            self.logger.error("Incomplete information for entry %s:%s; cannot verify" \
                                              % (inst.tag, inst.get('name')))
                            return False
        return True

    def FindExtraPackages(self):
        '''
           Find extra packages
        '''
        packages = [entry.get('name') for entry in self.getSupportedEntries()]
        extras = []

        for (name, instances) in self.installed.iteritems():
            if name not in packages:
                extra_entry = Bcfg2.Client.XML.Element('Package', name=name, type=self.pkgtype)
                for installed_inst in instances:
                    self.logger.info("Extra Package %s %s." % \
                                     (name, self.str_evra(installed_inst)))
                    Bcfg2.Client.XML.SubElement(extra_entry, \
                                                'Instance', 
                                                epoch = str(installed_inst.get('epoch', '')),\
                                                version = installed_inst.get('version'), \
                                                release = installed_inst.get('release'), \
                                                arch = installed_inst.get('arch', ''))
                    extras.append(extra_entry)
        return extras


    def FindExtraInstances(self, pkg_entry, installed_entry):
        '''
            Check for installed instances that are not in the config.
            Return a Package Entry with Instances to remove, or None if there
            are no Instances to remove.
        '''
        name = pkg_entry.get('name')
        extra_entry = Bcfg2.Client.XML.Element('Package', name=name, type=self.pkgtype)
        instances = [inst for inst in pkg_entry if inst.tag == 'Instance' or inst.tag == 'Package']
        if name in self.installOnlyPkgs:
            for installed_inst in installed_entry:
                not_found = True
                for inst in instances:
                    if self.pkg_vr_equal(inst, installed_inst) or \
                       self.inst_evra_equal(inst, installed_inst):
                        not_found = False
                        break
                if not_found == True:
                    # Extra package.
                    self.logger.info("Extra InstallOnlyPackage %s %s." % \
                                               (name, self.str_evra(installed_inst)))
                    tmp_entry = Bcfg2.Client.XML.SubElement(extra_entry, 'Instance', \
                                     version = installed_inst.get('version'), \
                                     release = installed_inst.get('release'))
                    if installed_inst.get('epoch', None) != None:
                        tmp_entry.set('epoch', str(installed_inst.get('epoch')))
                    if installed_inst.get('arch', None) != None:
                        tmp_entry.set('arch', installed_inst.get('arch'))
        else:
            # Normal package, only check arch.
            for installed_inst in installed_entry:
                not_found = True
                for inst in instances:
                    if installed_inst.get('arch', None) == inst.get('arch', None) or\
                       inst.tag == 'Package':
                        not_found = False
                        break
                if not_found:
                    self.logger.info("Extra Normal Package Instance %s %s" % \
                                                        (name, self.str_evra(installed_inst)))
                    Bcfg2.Client.XML.SubElement(extra_entry, \
                                     'Instance', 
                                     epoch = str(installed_inst.get('epoch', '')),\
                                     version = installed_inst.get('version'), \
                                     release = installed_inst.get('release'), \
                                     arch = installed_inst.get('arch', ''))

        if len(extra_entry) == 0:
            extra_entry = None

        return extra_entry

    def Inventory(self, structures=[]):
        '''
           Wrap the Tool.Inventory() method with its own rpm.TransactionSet() 
           and an explicit closeDB() as the close wasn't happening and DB4 
           locks were getting left behind on the RPM database creating a nice 
           mess.

           ***** Do performance comparison with the transctionset/closeDB
                 moved into rpmtools, which would mean a transactionset/closeDB
                 per VerifyPackage() call (meaning one per RPM package) rather 
                 than one for the whole system.
        '''
        self.vp_ts = rpmtools.rpmtransactionset()
        Bcfg2.Client.Tools.Tool.Inventory(self)
        # Tool is still an old style class, so super doesn't work. Change it.
        #super(RPMng, self).Inventory()
        self.vp_ts.closeDB()

    def str_evra(self, instance):
        '''
            Convert evra dict entries to a string.
        '''
        return '%s:%s-%s.%s' % (instance.get('epoch', '*'), instance.get('version', '*'),
                                instance.get('release', '*'), instance.get('arch', '*'))

    def pkg_vr_equal(self, config_entry, installed_entry):
        '''
            Compare old style entry to installed entry.  Which means ignore
            the epoch and arch.
        '''
        if (config_entry.tag == 'Package' and \
            config_entry.get('version') == installed_entry.get('version') and \
            config_entry.get('release') == installed_entry.get('release')):
            return True
        else:
            return False

    def inst_evra_equal(self, config_entry, installed_entry):
        '''
            Compare new style instance to installed entry.
        '''

        if config_entry.get('epoch', None) != None:
            epoch = int(config_entry.get('epoch'))
        else:
            epoch = None

        if (config_entry.tag == 'Instance' and\
            epoch == installed_entry.get('epoch', None) and \
            config_entry.get('version') == installed_entry.get('version') and \
            config_entry.get('release') == installed_entry.get('release') and \
            config_entry.get('arch', None) == installed_entry.get('arch', None)):
            return True
        else:
            return False

    def getinstalledgpg(self):
        ''' 
           Create a list of installed GPG key IDs.

           The pgp-pubkey package version is the least significant 4 bytes
           (big-endian) of the key ID which is good enough for our purposes.
        ''' 
        init_ts = rpmtools.rpmtransactionset()
        init_ts.setVSFlags(rpm._RPMVSF_NODIGESTS|rpm._RPMVSF_NOSIGNATURES)
        gpg_hdrs = rpmtools.getheadersbykeyword(init_ts, **{'name':'gpg-pubkey'})
        keyids = [ header[rpm.RPMTAG_VERSION] for header in gpg_hdrs]
        keyids.append('None')
        init_ts.closeDB()
        del init_ts
        return keyids