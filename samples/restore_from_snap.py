#!/usr/bin/env python
#
# checks for a specific snapshot for a named VMs, and restores it
#

import atexit
import configargparse
import getpass
import re
import ssl
import sys
import time

from pyVim import connect
from pyVim.task import WaitForTask
from pyVmomi import vmodl, vim

# creds etc can be stores in a file
DEFAULT_CONFIG_FILENAME = ".restore_config"


class EsxTalker(object):
    """
    Class that handles talking to ESX and holds the various utility methods
    """

    def __init__(self, args):
        """
        Initialise the EsxTalker.
        :param args - the params passed to the program, which must include
                      the username, password and host for the vSphere
        """
        self.args = args  # as there may be more than just the esx creds
        if self.args.debug:  # see :)
            print "Debug mode"
        # magic to disable SSL cert checking
        s = None
        if args.insecure:
            s = ssl.SSLContext(ssl.PROTOCOL_TLSv1)
            s.verify_mode = ssl.CERT_NONE
        # OK - let's get a connection
        try:
            self.svc_inst = connect.SmartConnect(host=args.host,
                                                 user=args.user,
                                                 pwd=args.password,
                                                 port=int(args.port),
                                                 sslContext=s)
            # incantation to close at the end
            atexit.register(connect.Disconnect, self.svc_inst)
            # verify that the connection has worked.
            self.sid = self.svc_inst.content.sessionManager.currentSession.key
            assert self.sid is not None, "Connection to ESX failed"
        except vmodl.MethodFault as error:
            print "Caught vmodl fault : " + error.msg
            sys.exit(1)
        self.content = self.svc_inst.RetrieveContent()

    def get_obj(self, vimtype, name):
        """
        Get the vsphere object associated with a given text name
        :param vimtype - type of object searched for.
        :param name - name of item searched for.
        :return matching object or None
        """
        obj = None
        container = self.content.viewManager.CreateContainerView(
            self.content.rootFolder, vimtype, True)
        for c in container.view:
            if c.name == name:
                return c
        return None

    def get_vm_by_name(self, name):
        """
        Get the VM object for the VM with the given name
        :param name - exact name of target VM
        :return matching VM or None
        """
        return self.get_obj([vim.VirtualMachine], name)

    def get_snapshots(self, rootlist):
        """
        Starting from the root list, return
        a list of snapshots.
        :param rootlist - the VM snapshot rootlist
        :return list of snapshots
        """
        results = []
        for s in rootlist:
            results.append(s)
            results += self.get_snapshots(s.childSnapshotList)
        return results

    def find_matching_snapshot(self, snapshots, regex):
        """
        Return the list of existing snapshots filtered using
        a regex - which can be a simple substring expected to
        be found in the name field.
        :param snapshots - list of snapshots
        :param regex - string with an expression to re.search on.
        :return filtered list
        """
        if snapshots is None:
            return None
        if len(snapshots) < 1:
            return None
        return [s for s in snapshots if re.search(regex, s.name)]

    def create_snapshot(self,
                        vmname,
                        snapname,
                        description="",
                        dumpMem=False,
                        quiesce=False):
        """
        Create a snapshot of a named VM
        :param vmname - VM to be snapshotted
        :param snapname - name to use for snapshot
        """
        vm = self.get_vm_by_name(vmname)
        assert vm is not None, "Did not find specified VM!"
        print "Creating snapshot %s for %s ..." % (snapname, vmname)
        if self.args.debug:
            print """
            DEBUG :
            WaitForTask(vm.CreateSnapshot(snap_name,
                                          description,
                                          dumpMem,
                                          quiesce))
            """
        else:
            WaitForTask(vm.CreateSnapshot(snapname,
                                          description,
                                          dumpMem,
                                          quiesce))

    def revert_to_snap(self, vmname, snapnameregex):
        """
        Revert the named VM to the named snapshot
        :param vmname - the name of the VM
        :param snapnameregex - the search pattern for the chosen snapshot
        """
        print "Get snapshots from %s ..." % vmname
        vm = self.get_vm_by_name(vmname)
        snaps = self.get_snapshots(vm.snapshot.rootSnapshotList)
        print "Finding snapshot ..."
        target_snap = self.find_matching_snapshot(snaps, snapnameregex)
        assert len(target_snap) == 1,\
            "More than one snap identified - confused!\n" +\
            "Please use a more unique string."
        print "Snap found matching name ..."
        thesnap2use = target_snap[0]
        print "thesnap2use = ", thesnap2use
        assert thesnap2use is not None
        if self.args.debug:
            print "DEBUG : This task will cause the VM to revert",
            thesnap2use.snapshot.RevertToSnapshot_Task
        else:
            WaitForTask(thesnap2use.snapshot.RevertToSnapshot_Task())


def get_args():
    """
    Get command line args from the user.
    Uses configargparse so can use combination of command line,
    config file and env vars
    """
    parser = configargparse.ArgParser(
        config_file_parser_class=configargparse.YAMLConfigFileParser,
        default_config_files=[DEFAULT_CONFIG_FILENAME],
        description='Tool to manipulate VM snapshots on ESX cluster')
    parser.add_argument('-c', '--my-config',
                        required=False,
                        is_config_file=True,
                        help='config file path')
    parser.add_argument('-H', '--host',
                        required=True,
                        action='store',
                        help='vSphere service to connect to')
    parser.add_argument('-P', '--port',
                        type=int,
                        default=443,
                        action='store',
                        help='Port to connect on')
    parser.add_argument('-u', '--user',
                        required=True,
                        action='store',
                        help='User name to use when connecting to host')
    parser.add_argument('-p', '--password',
                        required=False,
                        action='store',
                        env_var="ESX_PASSWORD",
                        help='Password to use when connecting to host')
    parser.add_argument('-v', '--vm_names',
                        required=True,
                        action='append',
                        env_var="VM_NAME",
                        default=[],
                        help='VM name')
    parser.add_argument('-s', '--snap_name',
                        required=True,
                        env_var="SNAP_NAME",
                        action='store',
                        help="String to use when searching snapshot names")
    parser.add_argument('-d', '--debug',
                        required=False,
                        action='store_true',
                        env_var="DEBUG",
                        help='Debug mode - do not do the revert')
    parser.add_argument('-S', '--save_first',
                        required=False,
                        action='store_true',
                        help='Before doing a revert, snapshot current state.')
    parser.add_argument('-i', '--insecure',
                        required=False,
                        action='store_true',
                        help='Insecure mode - ' +
                        'do not validate the SSL certificate')
    args = parser.parse_args()
    if not args.password:
        args.password = getpass.getpass(
            prompt='Enter password for host %s and user %s: ' %
                   (args.host, args.user))
    return args


def main():
    """
    """

    args = get_args()
    et = EsxTalker(args)

    print "Get VM by names =", args.vm_names
    for vmname in args.vm_names:
        if args.save_first:
            # prior to winding back, create a snapshot of now - just in case
            print "snapshotting prior to revert"
            new_snap_name = "%s_PREREVERT_%s" % (vmname, str(time.time()))
            description = "Snapshot prior to revert operation"
            et.create_snapshot(vmname, new_snap_name, description)
        # now do the revert
        et.revert_to_snap(vmname, args.snap_name)


if __name__ == "__main__":
    main()
