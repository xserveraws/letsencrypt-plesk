import augeas
import subprocess
import re
import os
import socket

BASE_DIR = "/etc/apache2/"

class VH(object):
    def __init__(self, vh_path, vh_addrs):
        self.path = vh_path
        self.addrs = vh_addrs
        self.names = []

    def set_names(self, listOfNames):
        self.names = listOfNames

    def add_name(self, name):
        self.names.append(name)

class Configurator(object):
    
    def __init__(self):
        # TODO: this instantiation can be optimized to only load Httd 
        #       relevant files
        # Set Augeas flags to save backup
        self.aug = augeas.Augeas(None, None, 1 << 0)
        self.vhosts = self.get_virtual_hosts()
        # httpd_files - All parsable Httpd files
        # add_transform overwrites all currently loaded files so we must 
        # maintain state
        self.httpd_files = []
        for m in self.aug.match("/augeas/load/Httpd/incl"):
            self.httpd_files.append(self.aug.get(m))
        self.mod_files = set()

    # TODO: This function can be improved to ensure that the final directives 
    # are being modified whether that be in the include files or in the 
    # virtualhost declaration - these directives can be overwritten
    def deploy_cert(self, vhost, cert, key, cert_chain=None):
        """
        Currently tries to find the last directives to deploy the cert in
        the given virtualhost.  If it can't find the directives, it searches
        the "included" confs.  The function verifies that it has located 
        the three directives and finally modifies them to point to the correct
        destination
        TODO: Should add/remove chain directives 
        """
        search = {}
        path = {}
        search["cert_file"] = "//* [self::directive='SSLCertificateFile'][last()]/arg"
        search["cert_key"] = "//*[self::directive='SSLCertificateKeyFile'][last()]/arg"
        
        path["cert_file"] = self.aug.match(vhost.path + search["cert_file"])
        path["cert_key"] = self.aug.match(vhost.path + search["cert_key"])

        # Only include if a certificate chain is specified
        if cert_chain is not None:
            search["cert_chain"] = "//*[self::directive='SSLCertificateChainFile'][last()]/arg"
            path["cert_chain"] = self.aug.match(vhost.path + search["cert_chain"])

            includeArgs = self.aug.match(vhost.path + "//*[self::directive='Include']/arg")
        for k in path.iterkeys():
            if len(path[k]) == 0:
                # Directive not found... search the includes
                # Search in reverse because it is the last directive that 
                # matters
                for includeArg in reversed(includeArgs):  
                    path[k] = self.search_include(includeArg, search[k])
                    if len(path[k]) > 0:
                        break
        
        for k in path.iterkeys():
            if len(path[k]) == 0:
                # Throw some "can't find all of the directives error"
                print "DEBUG - Error: cannot find ", search[k]
                print "DEBUG - in ", vhost.path
                print "VirtualHost was not modified"
                # Presumably break here so that the virtualhost is not modified
                return False
            
        self.aug.set(path["cert_file"][0], cert)
        self.aug.set(path["cert_key"][0], key)
        if cert_chain is not None:
            self.aug.set(path["cert_chain"][0], cert_chain)
        
        return self.save("Virtual Server - deploying certificate")

    def choose_virtual_host(self, name):
        """
        TODO: Finish this function correctly
        TODO: This should return vhost of :443 if both 80 and 443 exist
              This is currently just a very basic demo version
        """
        for v in self.vhosts:
            for n in v.names:
                # TODO: Or a converted FQDN address
                if n == name:
                    return v
        for v in self.vhosts:
            for a in v.addrs:
                tup = a.partition(":")
                if tup[0] == name:
                    return v
        for v in self.vhosts:
            for a in v.addrs:
                if a == "_default_:443":
                    return v
        return None
                    
    def get_all_names(self):
        """
        Returns all names found in the Apache Configuration
        Returns all ServerNames, ServerAliases, and reverse DNS entries for
        virtual host addresses
        """
        all_names = []
        for v in self.vhosts:
            all_names.extend(v.names)
            for a in v.addrs:
                a_tup = a.split(":")
                try:
                    socket.inet_aton(a_tup[0])
                    all_names.append(socket.gethostbyaddr(a_tup[0])[0])
                except (socket.error, socket.herror, socket.timeout):
                    continue

        return all_names

    def __add_servernames(self, host):
        """
        Helper function for get_virtual_hosts()
        """
        # This is case sensitive, but Apache is case insensitve
        # Spent a bunch of time trying to get case insensitive search
        # it should be possible as of .7 with /i or 'append i' but I have been
        # unsuccessful thus far
        nameMatch = self.aug.match(host.path + "//*[self::directive=~regexp('[sS]erver[nN]ame')] | " + host.path + "//*[self::directive=~regexp('[sS]erver[aA]lias')]")
        for name in nameMatch:
            args = self.aug.match(name + "/*")
            for arg in args:
                host.add_name(self.aug.get(arg))
                

    def get_virtual_hosts(self):
        #Search sites-available, httpd.conf for possible virtual hosts
        paths = self.aug.match("/files" + BASE_DIR + "sites-available//VirtualHost")
        vhs = []
        for p in paths:
            addrs = []
            args = self.aug.match(p + "/arg")
            for arg in args:
                addrs.append(self.aug.get(arg))
            vhs.append(VH(p, addrs))

        for host in vhs:
            self.__add_servernames(host)

        return vhs

    def is_name_vhost(self, addr):
        # search for NameVirtualHost directive for ip_addr
        # check httpd.conf, ports.conf, 
        # note ip_addr can be FQDN
        paths = self.find_directive("NameVirtualHost", None)
        name_vh = []
        for p in paths:
            name_vh.append(self.aug.get(p))
        
        # TODO: Check ramifications for FQDN/IP_ADDR mismatch overlap
        #       ie. NameVirtualHost FQDN ... <VirtualHost IPADDR>
        #       Does adding additional NameVirtualHost directives cause problems
        # Check for exact match
        for vh in name_vh:
            if vh == addr:
                return True
        # Check for general IP_ADDR name_vh
        tup = addr.partition(":")
        for vh in name_vh:
            if vh == tup[0]:
                return True
        # Check for straight wildcard name_vh
        for vh in name_vh:
            if vh == "*":
                return True
        # NameVirtualHost directive should be added for this address
        return False

    def add_name_vhost(self, addr):
        """
        Adds NameVirtualHost directive for given address
        Directive is added to ports.conf unless 
        """
        aug_file_path = "/files" + BASE_DIR + "ports.conf"
        self.add_dir_to_ifmodssl(aug_file_path, "NameVirtualHost", addr)
        
        if len(self.find_directive("NameVirtualHost", addr)) == 0:
            print "ports.conf is not included in your Apache config... "
            print "Adding NameVirtualHost directive to httpd.conf"
            self.add_dir_to_ifmodssl("/files" + BASE_DIR + "httpd.conf", "NameVirtualHost", addr)
            

    def add_dir_to_ifmodssl(self, aug_conf_path, directive, val):
        # TODO: Add error checking code... does the path given even exist?
        #       Does it throw exceptions?
        ifModPath = self.get_ifmod(aug_conf_path, "mod_ssl.c")
        # IfModule can have only one valid argument, so append after
        self.aug.insert(ifModPath + "arg", "directive", False)
        nvhPath = ifModPath + "directive[1]"
        self.aug.set(nvhPath, directive)
        self.aug.set(nvhPath + "/arg", val)

    def make_server_sni_ready(self, vhost):
        """
        Checks to see if the server is ready for SNI challenges
        """
        # Check if mod_ssl is loaded
        if not self.check_ssl_loaded():
            print "Please load the SSL module with Apache"
            return False

        # Check for Listen 443
        # TODO: This could be made to also look for ip:443 combo
        # TODO: Need to search only open directives and IfMod mod_ssl.c
        if len(self.find_directive("Listen", "443")) == 0:
            print self.find_directive("Listen", "443")
            print "Setting the Apache Server to Listen on port 443"
            self.add_dir_to_ifmodssl("/files" + BASE_DIR + "ports.conf", "Listen", "443")

        # Check for NameVirtualHost
        # First see if any of the vhost addresses is a _default_ addr
        for addr in vhost.addrs:
            tup = addr.partition(":") 
            if tup[0] == "_default_":
                if not self.is_name_vhost("*:443"):
                    print "Setting all VirtualHosts on *:443 to be name based virtual hosts"
                    self.add_name_vhost("*:443")
                return True
        # No default addresses... so set each one individually
        for addr in vhost.addrs:
            if not self.is_name_vhost(addr):
                print "Setting VirtualHost at", addr, "to be a name based virtual host"
                self.add_name_vhost(addr)

        return True

    def get_ifmod(self, aug_conf_path, mod):
        ifMods = self.aug.match(aug_conf_path + "/IfModule/*[self::arg='" + mod + "']")
        if len(ifMods) == 0:
            self.aug.set(aug_conf_path + "/IfModule[last() + 1]", "")
            self.aug.set(aug_conf_path + "/IfModule[last()]/arg", mod)
            ifMods = self.aug.match(aug_conf_path + "/IfModule/*[self::arg='" + mod + "']")
        # Strip off "arg" at end of first ifmod path
        return ifMods[0][:len(ifMods[0]) - 3]
    
    def add_dir(self, aug_conf_path, directive, arg):
        self.aug.set(aug_conf_path + "/directive[last() + 1]", directive)
        self.aug.set(aug_conf_path + "/directive[last()]/arg", arg)
        
    def find_directive(self, directive, arg=None, start="/files"+BASE_DIR+"apache2.conf"):
        """
        Recursively searches through config files to find directives
        TODO: arg should probably be a list
        """
        if arg is None:
            matches = self.aug.match(start + "//* [self::directive='"+directive+"']/arg")
        else:
            matches = self.aug.match(start + "//* [self::directive='"+directive+"']/* [self::arg='" + arg + "']")
            
        includes = self.aug.match(start + "//* [self::directive='Include']/* [label()='arg']")

        for include in includes:
            matches.extend(self.find_directive(directive, arg, self.get_include_path(self.strip_dir(start[6:]), self.aug.get(include))))
        
        return matches

    def strip_dir(self, path):
        """
        Precondition: file_path is a file path, ie. not an augeas section 
                      or directive path
        Returns the current directory from a file_path along with the file
        """
        index = path.rfind("/")
        if index > 0:
            return path[:index+1]
        # No directory
        return ""

    def get_include_path(self, cur_dir, arg):
        """
        Converts an Apache Include directive argument into an Augeas 
        searchable path
        Returns path string
        """
        # Standardize the include argument based on server root
        if not arg.startswith("/"):
            arg = cur_dir + arg
        # conf/ is a special variable for ServerRoot in Apache
        elif arg.startswith("conf/"):
            arg = BASE_DIR + arg[5:]
        # TODO: Test if Apache allows ../ or ~/ for Includes
 
        # Attempts to add a transform to the file if one does not already exist
        self.parse_file(arg)
        
        # Argument represents an fnmatch regular expression, convert it
        if "*" in arg or "?" in arg:
            postfix = ""
            splitArg = arg.split("/")
            for idx, split in enumerate(splitArg):
                # * and ? are the two special fnmatch characters 
                if "*" in split or "?" in split:
                    # Check to make sure only expected characters are used
                    validChars = re.compile("[a-zA-Z0-9.*?]*")
                    matchObj = validChars.match(split)
                    if matchObj.group() != split:
                        print "Error: Invalid regexp characters in", arg
                        return []
                    # Turn it into a augeas regex
                    splitArg[idx] = "* [label() =~ regexp('" + self.fnmatch_to_re(split) + "')]"
            # Reassemble the argument
            arg = "/".join(splitArg)
                    
        # If the include is a directory, just return the directory as a file
        if arg.endswith("/"):
            return "/files" + arg[:len(arg)-1]
        return "/files"+arg

    def check_ssl_loaded(self):
        """
        Checks apache2ctl to get loaded module list
        """
        try:
            p = subprocess.check_output(["sudo", "apache2ctl", "-M"], stderr=open("/dev/null"))
        except:
            print "Error accessing apache2ctl for loaded modules!"
            print "This may be caused by an Apache Configuration Error"
            return False
        if "ssl_module" in p:
            return True
        return False

    def enable_site(self, avail_fp):
        """
        Enables an available site, Apache restart required
        """
        if "/sites-available/" in avail_fp:
            index = avail_fp.rfind("/")
            os.symlink(avail_fp, BASE_DIR + "sites-enabled/" + avail_fp[index:])
            return True
        return False
    
    def enable_mod_ssl(self):
        """
        Enables mod_ssl
        TODO: TEST
        """
        # Use check_output so the command will finish before reloading the server
        subprocess.check_output(["sudo", "a2enmod", "ssl"])
        subprocess.call(["sudo", "/etc/init.d/apache2", "reload"])
        """
        a_conf = BASE_DIR + "mods-available/ssl.conf"
        a_load = BASE_DIR + "mods-available/ssl.load"
        if os.path.exists(a_conf) and os.path.exists(a_load):
            os.symlink(a_conf, BASE_DIR + "mods-enabled/ssl.conf")
            os.symlink(a_load, BASE_DIR + "mods-enabled/ssl.load")
            return True
        return False
        """

    # Go down the Include rabbit hole
    # TODO: REMOVE... use find_directive
    def search_include(self, includeArg, searchStr):
        print "Deprecated Function... please use find_directive"
        # Standardize the include argument based on server root
        arg = includeArg
        if not includeArg.startswith("/"):
            arg = BASE_DIR + includeArg

        # Test if augeas included file for Httpd.lens
        incTest = aug.match("/files" + arg + "/*")
        if len(incTest) == 0:
            # Load up file
            self.aug.add_transform("Httpd.lns", arg)
            self.aug.load()
            
        return self.aug.match("/files" + arg + searchStr)

    def fnmatch_to_re(self, cleanFNmatch):
        """
        Method converts Apache's basic fnmatch to regular expression
        """
        regex = ""
        for letter in cleanFNmatch:
            if letter == '.':
                regex = regex + "\."
            elif letter == '*':
                regex = regex + ".*"
            # According to apache.org ? shouldn't appear
            # but in case it is valid...
            elif letter == '?':
                regex = regex + "."
            else:
                regex = regex + letter
        return regex

    def parse_file(self, file_path):
        # Test if augeas included file for Httpd.lens
        # Note: This works for augeas globs, ie. *.conf
        incTest = self.aug.match("/augeas/load/Httpd/incl [. ='" + file_path + "']")
        if len(incTest) == 0:
            # Load up files
            self.httpd_files.append(file_path)
            self.aug.add_transform("Httpd.lns", self.httpd_files)
            self.aug.load()

    def save(self, mod_conf="Augeas Configuration", reversible=False):
        try:
            self.aug.save()
            if reversible:
                # Retrieve list of modified files
                save_paths = self.aug.match("/augeas/events/saved")
                for path in save_paths:
                    # Strip off /files
                    filename = self.aug.get(path)[6:]
                    if filename in self.mod_files:
                        # Output a warning... hopefully this can be avoided so more
                        # complex code doesn't have to be written
                        print "Reversible file has been overwritten -", filename
                    else:
                        self.mod_files.add(filename)
            return True
        except IOError:
            print "Unable to save file - ", mod_conf
            print "Is the script running as root?"
        return False

    def revert_config(self):
        """
        This function should reload the users original configuration files
        """
        for f in self.mod_files:
            print "reverting", f
            os.rename(f + ".augsave", f)
        self.aug.load()
        self.mod_files.clear()
        

def recurmatch(aug, path):
    if path:
        if path != "/":
            val = aug.get(path)
            if val:
                yield (path, val)

        for i in aug.match(path + "/*"):
            for x in recurmatch(aug, i):
                yield x

def main():
    config = Configurator()
    for v in config.vhosts:
        print v.addrs
        for name in v.names:
            print name

    for m in config.find_directive("Listen", "443"):
        print "Directive Path:", m, "Value:", config.aug.get(m)

    for v in config.vhosts:
        for a in v.addrs:
            print "Address:",a, "- Is name vhost?", config.is_name_vhost(a)

    print config.get_all_names()

    config.parse_file("/etc/apache2/ports_test.conf")
    
    
    #for m in config.aug.match("/augeas/load/Httpd/incl"):
    #    print m, config.aug.get(m)
    #config.add_name_vhost("example2.com:443")
    #for vh in config.vhosts:
        #if len(vh.names) > 0:
            #config.deploy_cert(vh, "/home/james/Documents/apache_choc/default.crt", "/home/james/Documents/apache_choc/testing.key")

#print config.search_include("/etc/apache2/choc_sni_cert_chal_test.conf", "/*")

if __name__ == "__main__":
    main()

    