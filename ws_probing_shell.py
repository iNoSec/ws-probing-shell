#!/usr/bin/env python
"""
    Interactive shell in order to probe/analyze a WebSocket endpoint
"""

import time
import cmd
import argparse
import json
import hashlib
from string import Template
from collections import OrderedDict
from urllib.parse import unquote
import colorama
from termcolor import colored
from websocket import create_connection
from websocket import WebSocketConnectionClosedException
from websocket import WebSocketException
from tabulate import tabulate


class WSProbingShell(cmd.Cmd):
    """
    Interactive shell in order to probe/analyze a WebSocket endpoint
    """
    def __init__(self):
        """
        Constructor
        """
        super(WSProbingShell, self).__init__()
        # WebSocket connection
        self.__client = None
        # Collection of last exchanges with the WS server during the last command execution
        # KEY is the exchange occurence number (int) and the VALUE is a dict for which:
        #   Value associated with Key named "REQUEST" is the request sent
        #   Value associated with Key named "RESPONSE" is the response received
        #   Value associated with Key named "RESPONSE_TIME" is the response time associated with the exchange
        #   Value associated with Key named "REQUEST_LENGTH" is the length of the request sent
        #   Value associated with Key named "RESPONSE_LENGTH" is the length of the response received
        #   Value associated with Key named "IS_ERROR" is a flag to indicate if the request meet WS error during sending
        self.__exchanges = {}
        # Save connection parameters in order to reopen connection later in case of need
        self.__client_connection_parameters = None

    def do_connect(self, line):
        """
        Establish a WebSocket connection with the specified endpoint

        Syntax:
        connect -t [endpoint] -o [origin] -e [extra_http_headers] -s [subprotocols]

        Examples:
        connect -t ws://echo.websocket.org
        connect -t ws://echo.websocket.org -o http://mysite.com
        connect -t ws://echo.websocket.org -o http://mysite.com -e Cookie=xxxx§User=yyyy
        connect -t ws://echo.websocket.org -o http://mysite.com -e Cookie=xxxx§User=yyyy -p authentication§session

        Parameters:
        endpoint: WS endpoint URL
        origin: Value of the origin header to fake the originator of the connection
        extra_http_headers: List of HTTP headers url encoded to add during the connection handshake (format: HEADER1_NAME=HEADER2_VALUE§HEADERx_NAME=HEADERx_VALUE)
        subprotocols: List of supported WS subprotocols in order of decreasing preference (format: protocol1§protocolx)
        """
        try:
            # Define parser for command line arguments
            parser = argparse.ArgumentParser()
            parser.add_argument('-t', action="store", dest="endpoint")
            parser.add_argument('-o', action="store", dest="origin", default=None)
            parser.add_argument('-e', action="store", dest="extra_http_headers", default=None)
            parser.add_argument('-p', action="store", dest="subprotocols", default=None)
            # Handle empty argument and mandatory arguments case
            if line is None or line.strip() == "" or "-t" not in line:
                print(colored("[!] Missing parameters !", "yellow", attrs=[]))
            else:
                # Parse command line
                args = parser.parse_args(line.split(" "))
                # Build custom headers map
                extra_headers = {}
                if args.extra_http_headers is not None:
                    for pair in args.extra_http_headers.split("§"):
                        parts = pair.split("=")
                        extra_headers[parts[0]] = parts[1]
                # Build subprotocols list
                subprotocols_set = []
                if args.subprotocols is not None:
                    for subprotocol in args.subprotocols.split("§"):
                        subprotocols_set.append(subprotocol)
                # Connect to endpoint and send a message to validate the connection
                print(colored("[*]    Connecting...", "cyan", attrs=[]))
                self.__client = create_connection(url=args.endpoint, timeout=10, header=extra_headers, origin=args.origin, subprotocols=subprotocols_set)
                self.__client.send("hello")
                if self.__client.recv() is not None:
                    # Save connection parameters in order to reopen connection later in case of need
                    self.__client_connection_parameters = line
                    print(colored("[*]    Connected.", "cyan", attrs=[]))
                else:
                    print(colored("[!]    Connection state cannot be confirmed !", "yellow", attrs=[]))
        except Exception as error:
            print(colored("[!]    Connection failed: %s" % error, "red", attrs=[]))

    def do_replay(self, line):
        """
        Replay a specified message a specified number of times

        Syntax:
        replay -m [path_to_message_file] -n [repetition_count]

        Example:
        replay -m /tmp/message.txt -n 20

        Parameters:
        path_to_message_file: Path to the file (text format) containing the message to replay, no space in path.
        repetition_count: Number of time that the message must be send
        """
        try:
            # Define parser for command line arguments
            parser = argparse.ArgumentParser()
            parser.add_argument('-m', action="store", dest="path_to_message_file")
            parser.add_argument('-n', action="store", dest="repetition_count", type=int)
            # Handle empty argument and mandatory arguments case
            if line.strip() == "" or "-m" not in line or "-n" not in line:
                print(colored("[!] Missing parameters !", "yellow", attrs=[]))
            else:
                # Parse command line
                args = parser.parse_args(line.split(" "))
                # Read message
                print(colored("[*] Read message...", "cyan", attrs=[]))
                with open(args.path_to_message_file, "r") as m_file:
                    message = m_file.read()
                print(colored("[*] Message readed.", "cyan", attrs=[]))
                # Check if connection is still available
                self.__check_connection_availability()
                # Build the list of messages to send
                messages_list = [message] * args.repetition_count
                # Send message(s)
                self.__exchanges.clear()
                self.__send_messages(messages_list)
                # Save exchanges data to a local file
                filename = "exchanges_replay.json"
                print(colored("[*] Exchanges saved to file '%s'." % filename, "cyan", attrs=[]))
                self.__store_exchanges_to_file(filename)
                print(colored("[*] Use commands 'analyze' or 'search' to run a analysis on the exchanges data in order to spot interesting elements.", "cyan", attrs=[]))
        except Exception as error:
            print(colored("[!] Replay failed: %s" % error, "red", attrs=[]))

    def do_fuzz(self, line):
        """
        Send fuzzing message based on a message template and a set of files containing payloads for each positions in the template message

        Syntax:
        fuzz -m [path_to_template_message_file] -p [path_to_payload_message_file_1] [path_to_payload_message_file_x]

        Example:
        fuzz -m /tmp/message_template.txt -p /tmp/message_payload_1.txt /tmp/message_payload_2.txt

        Message template example:
        Hello $payload_1 from $payload_2 !

        Parameters:
        path_to_template_message_file: Path to the file (text format) containing the template of the message, no space in path.
                                       Placeholders use the Python templating syntax:
                                       Use $payload_1 for payload coming from payload file 1 and so on...
                                       Use $$ to escape the $ character if your original text need to contains a $.
        path_to_payload_message_file_x: Path to the file (text format) containing the payload (one by line) to use for the current position (x here), no space in path.
        """
        try:
            # Define parser for command line arguments
            parser = argparse.ArgumentParser()
            parser.add_argument('-m', action="store", dest="path_to_template_message_file")
            parser.add_argument('-p', action="store", dest="payload_files", nargs="+")
            # Handle empty argument and mandatory arguments case
            if line.strip() == "" or "-m" not in line or "-p" not in line:
                print(colored("[!] Missing parameters !", "yellow", attrs=[]))
            else:
                # Parse command line
                args = parser.parse_args(line.split(" "))
                # Read message
                print(colored("[*] Read template message...", "cyan", attrs=[]))
                with open(args.path_to_template_message_file, "r") as m_file:
                    message_template = m_file.read()
                print(colored("[*] Message template readed.", "cyan", attrs=[]))
                # Check if connection is still available
                self.__check_connection_availability()
                # Build the list of messages to send
                print(colored("[*] Build the list of messages to send...", "cyan", attrs=[]))
                # -- Build a list of all payloads combinations with their placeholders
                payload_files_copy = args.payload_files[:]
                payloads = self.__build_fuzzing_dicts(payload_files_list=payload_files_copy, payload_combinations=[], current_position=1)
                # -- Build messages list
                message_template_object = Template(message_template)
                messages_list = []
                for payload_dict in payloads:
                    messages_list.append(message_template_object.safe_substitute(payload_dict))
                print(colored("[*] List of messages built (%s messages)." % len(messages_list), "cyan", attrs=[]))
                # Send message(s)
                self.__exchanges.clear()
                self.__send_messages(messages_list)
                # Save exchanges data to a local file
                filename = "exchanges_fuzzing.json"
                print(colored("[*] Exchanges saved to file '%s'." % filename, "cyan", attrs=[]))
                self.__store_exchanges_to_file(filename)
                print(colored("[*] Use commands 'analyze' or 'search' to run a analysis on the exchanges data in order to spot interesting elements.", "cyan", attrs=[]))
        except Exception as error:
            print(colored("[!] Fuzzing failed: %s" % error, "red", attrs=[]))

    def do_analyze(self, line):
        """
        Run a analysis on the exchanges data in order to spot interesting elements and print them (no parameter required).

        TODO: Add more analysis cases on exchanges !
        """
        if len(self.__exchanges) == 0:
            print(colored("[!] No exchanges available !", "yellow", attrs=[]))
        else:
            # Shortcuts
            exchanges = self.__exchanges
            total = len(exchanges)
            data_to_print = []
            exchanges_gathered = {}
            # 1) We analyze the exchanges response time by aggregate them on integer rounding time of the reponse time and sorting the aggregation result
            # Gather informations
            data_to_print.clear()
            exchanges_gathered.clear()
            for idx in range(0, total):
                response_time = int(exchanges[idx]["RESPONSE_TIME"])
                if response_time not in exchanges_gathered:
                    exchanges_gathered[response_time] = ""
                exchanges_gathered[response_time] += " " + str(idx)
            for k in OrderedDict(exchanges_gathered):
                data_to_print.append([k, exchanges_gathered[k].strip()])
            # Print result
            print(colored("[*] Exchanges aggregated by response time:", "cyan", attrs=[]))
            print(tabulate(headers=["Delay in seconds", "Exchange ID(s)"], tabular_data=data_to_print, tablefmt="grid", numalign="right", stralign="right"))
            # 2) We analyze the exchanges response in order to aggregate them for which the reponse is identical (same content)
            # Gather informations
            data_to_print.clear()
            exchanges_gathered.clear()
            for idx in range(0, total):
                response_identifier = hashlib.sha256(exchanges[idx]["RESPONSE"].encode("utf-8")).hexdigest()
                if response_identifier not in exchanges_gathered:
                    exchanges_gathered[response_identifier] = ""
                exchanges_gathered[response_identifier] += " " + str(idx)
            for k in exchanges_gathered:
                data_to_print.append([k, exchanges_gathered[k].strip()])
            # Print result
            print(colored("[*] Exchanges aggregated with identical response content:", "cyan", attrs=[]))
            print(tabulate(headers=["Response content digest (sha256 in hex)", "Exchange ID(s)"], tabular_data=data_to_print, tablefmt="grid", numalign="right", stralign="right"))

    def do_show(self, line):
        """
        Show the details of specified exchanges or all if not exchange id is provided

        ID start at zero

        Syntax:
        show
        show -e [exchange_id_1] [exchange_id_x]

        Examples:
        show
        show -e 0
        show -e 0 1 2

        Parameters:
        exchange_id_x: Exchange identifier (number)
        """
        try:
            # Define parser for command line arguments
            parser = argparse.ArgumentParser()
            parser.add_argument('-e', action="store", dest="exchange_ids", nargs="+", default=[])
            if len(self.__exchanges) == 0:
                print(colored("[!] No exchanges available !", "yellow", attrs=[]))
            else:
                # Parse command line
                args = None
                if "-e" in line:
                    args = parser.parse_args(line.split(" "))
                # Build the list of exchange ids to display
                if args is None or args.exchange_ids is None or len(args.exchange_ids) == 0:
                    ids = sorted(self.__exchanges.keys())
                else:
                    ids = args.exchange_ids
                # Build the list of data to print
                data_to_print = []
                for eid in ids:
                    eid_int = int(eid)
                    if eid_int not in self.__exchanges:
                        print(colored("[!] Exchange ID %s do not exists !" % eid, "yellow", attrs=[]))
                        continue
                    exchange = self.__exchanges[int(eid)]
                    # Add infos for REQUEST
                    fields = [eid, "REQUEST", "-", "-", exchange["REQUEST_LENGTH"], exchange["REQUEST"]]
                    data_to_print.append(fields)
                    # Add infos for RESPONSE
                    if exchange["IS_ERROR"]:
                        error_occur = "Yes"
                    else:
                        error_occur = "No"
                    fields = [eid, "RESPONSE", error_occur, exchange["RESPONSE_TIME"], exchange["RESPONSE_LENGTH"], exchange["RESPONSE"]]
                    data_to_print.append(fields)
                # Print result
                print(tabulate(headers=["Exchange ID", "Message type", "Error occur?", "Response delay in seconds", "Length", "Content"], tabular_data=data_to_print, tablefmt="grid", numalign="right", stralign="right"))
        except Exception as error:
            print(colored("[!] Show failed: %s" % error, "red", attrs=[]))

    def do_scan(self, line):
        """
        Scan a domain name using provided ports range or set in order to detect any WebSocket endpoint exposure
        
        Syntax:
        scan -t [domain] -p [ports_range_or_set] -u [uri]

        Examples:
        scan -t dvws.local -p 8000-9000
        scan -t dvws.local -p 8080,8089,9096
        scan -t dvws.local -p 8999
        scan -t dvws.local -p 8000-9000 -u /authenticate-user
        scan -t dvws.local -p 8080,8089,9096 -u /authenticate-user
        scan -t dvws.local -p 8999 -u /authenticate-user
        
        Parameters:
        domain: Domain name to scan for WebSocket endpoint exposure
        ports_range_or_set: List of ports to scan in "range" format (using START_PORT-END_PORT expression) or in "set" format (using PORT1,PORT2,PORTx expression).  
        uri: URI to append to domain name if needed. Can be used for example if you know a site but you want to check if some WebSocket endpoint are exposed. 
        """
        try:
            # Define parser for command line arguments
            parser = argparse.ArgumentParser()
            parser.add_argument('-t', action="store", dest="domain")
            parser.add_argument('-p', action="store", dest="ports")
            parser.add_argument('-u', action="store", dest="uri", default="")
            # Handle empty argument and mandatory arguments case
            if line.strip() == "" or "-t" not in line or "-p" not in line:
                print(colored("[!] Missing parameters !", "yellow", attrs=[]))
            else:
                # Parse command line
                args = parser.parse_args(line.split(" "))
                # Extract the list of ports to scan
                if "," in args.ports:
                    ports_to_scan = args.ports.split(",")
                elif "-" in args.ports:
                    parts = args.ports.strip().split("-")
                    start = int(parts[0])
                    end = int(parts[1])
                    ports_to_scan = range(start, end, 1)
                else:
                    ports_to_scan = [args.ports]
                # Perform scan
                print(colored("[*] Start scanning of %s ports..." % len(ports_to_scan), "cyan", attrs=[]))
                protocols = ["ws://", "wss://"]
                for port in ports_to_scan:
                    for protocol in protocols:
                        try:
                            target = protocol + args.domain + ":" + str(port) + args.uri
                            test_connection = create_connection(url=target, timeout=3)
                            print(colored("[*]    Port %s is available using protocol '%s'." % (port, protocol), "cyan", attrs=[]))
                            test_connection.close()
                        except (WebSocketException, IOError):
                            pass
        except Exception as error:
            print(colored("[!] Scan failed: %s" % error, "red", attrs=[]))

    def do_search(self, line):
        """
        Search for the presence of one or several words in exchanges responses

        Syntax:
        search -w [word_1] [word_x]
        search -i -w [word_1] [word_x]

        Examples:
        search -w test123 SQLException
        search -i -w test123 SQLException
        search -i -w OutOfMemory
        search -i -w hello%20world

        Parameters:
        word_x: Word to search in exchanges responses collection
                Use %20 to encode a space in word that need to contains a space

        Option "-i" is used to perform a case insensitive research
        """
        try:
            # Define parser for command line arguments
            parser = argparse.ArgumentParser()
            parser.add_argument('-w', action="store", dest="words", nargs="+")
            parser.add_argument('-i', action="store_true", dest="case_insensitive")
            # Handle empty argument and mandatory arguments case
            if line.strip() == "" or "-w" not in line:
                print(colored("[!] Missing parameters !", "yellow", attrs=[]))
            else:
                if len(self.__exchanges) == 0:
                    print(colored("[!] No exchanges available !", "yellow", attrs=[]))
                else:
                    # Parse command line
                    args = parser.parse_args(line.split(" "))
                    # Perform search
                    found = {}
                    for idx in range(0, len(self.__exchanges)):
                        resp = self.__exchanges[idx]["RESPONSE"]
                        if args.case_insensitive:
                            resp = resp.lower()
                        for word in args.words:
                            searched_word = unquote(word)
                            if args.case_insensitive:
                                searched_word = searched_word.lower()
                            if searched_word in resp:
                                if word not in found:
                                    found[word] = ""
                                found[word] += " " + str(idx)
                    # Save exchanges search to a local file
                    filename = "exchanges_searching.json"
                    print(colored("[*] Exchanges search saved to file '%s'." % filename, "cyan", attrs=[]))
                    self.__store_data_to_file(found, filename)
                    # Print result
                    data_to_print = []
                    for word in found:
                        data_to_print.append([word, found[word].strip()])
                    print(colored("[*] Words founds:", "cyan", attrs=[]))
                    print(tabulate(headers=["Word", "Exchange ID(s)"], tabular_data=data_to_print, tablefmt="grid", numalign="right", stralign="right"))
        except Exception as error:
            print(colored("[!] Search failed: %s" % error, "red", attrs=[]))

    def do_probe_request_length_limit(self, line):
        """
        Probe the WS server in order to determine the maximum length allowed for a request.

        Note: This command can also be used to identify if a request frequence limiter is in place on WS server.
        """
        try:
            # Check if connection is still available
            self.__check_connection_availability()
            # Send message increasing size at each step
            print(colored("[*] Send message with increasing length at each step...", "cyan", attrs=[]))
            max_length = 0
            max_probing_limit = 1000000000
            for idx in range(10, max_probing_limit, 10):
                try:
                    msg = "T" * idx
                    if idx % 1000 == 0:
                        print(colored("[*]    Length of %s characters reached, continue probing..." % idx, "cyan", attrs=[]))
                    self.__client.send(msg)
                except (WebSocketException, IOError):
                    # Remove 10 to have the previous iteration step idx value
                    max_length = idx - 10
                    # Exit because we have reach the limit
                    break
            if max_length > 0:
                print(colored("[*] Maximum request length limit identified to %s characters." % max_length, "cyan", attrs=[]))
            else:
                print(colored("[!] Maximum request length limit NOT identified BUT is superior to %s characters." % max_probing_limit, "yellow", attrs=[]))
        except Exception as error:
            print(colored("[!] Probing failed: %s" % error, "red", attrs=[]))

    def do_probe_connection_channels_supported(self, line):
        """
        Probe the WS server in order to determine if it support the secure or insecure channel connection using the following behavior:
        
        => If the initial connection was made with WSS secure protocol then the command will test the support for connection using WS insecure protocol
        
        => If the initial connection was made with WS insecure protocol then the command will test the support for connection using WSS secure protocol

        Note: Perform a initial connection using the "connect" command before to use this command in order to allow
        this command to know the connection context to use.     
        """
        try:
            # Check if connection context is defined
            if self.__client_connection_parameters is None:
                print(colored("[!] Perform a initial connection using the 'connect' command !", "yellow", attrs=[]))
            else:
                # Define parser for command line arguments stored in the connection context (same like for "connect" command)
                parser = argparse.ArgumentParser()
                parser.add_argument('-t', action="store", dest="endpoint")
                parser.add_argument('-o', action="store", dest="origin", default=None)
                parser.add_argument('-e', action="store", dest="extra_http_headers", default=None)
                parser.add_argument('-p', action="store", dest="subprotocols", default=None)
                # Parse command line stored in the connection context (same like for "connect" command)
                args = parser.parse_args(self.__client_connection_parameters.split(" "))
                # Build custom headers map
                extra_headers = {}
                if args.extra_http_headers is not None:
                    for pair in args.extra_http_headers.split("§"):
                        parts = pair.split("=")
                        extra_headers[parts[0]] = parts[1]
                # Build subprotocols list
                subprotocols_set = []
                if args.subprotocols is not None:
                    for subprotocol in args.subprotocols.split("§"):
                        subprotocols_set.append(subprotocol)
                # Perform probing
                target_endpoint = args.endpoint.lower()
                msg_prefix = "Secure"
                protocol_to_test = "wss://"
                if target_endpoint.startswith("wss://"):
                    msg_prefix = "Insecure"
                    protocol_to_test = "ws://"
                target_endpoint = protocol_to_test + target_endpoint.replace("wss://", "").replace("ws://", "")
                print(colored("[*] Test if WS server support '%s' %s protocol..." % (protocol_to_test, msg_prefix.lower()), "cyan", attrs=[]))
                try:
                    test_connection = create_connection(url=target_endpoint, timeout=10, header=extra_headers, origin=args.origin, subprotocols=subprotocols_set)
                    test_connection.send("hello")
                    if test_connection.recv() is not None:
                        print(colored("[*]    %s protocol '%s' supported." % (msg_prefix, protocol_to_test), "cyan", attrs=[]))
                    else:
                        print(colored("[*]    %s protocol '%s' not supported (no response to message sent)." % (msg_prefix, protocol_to_test), "cyan", attrs=[]))
                except (WebSocketException, IOError) as e:
                    print(colored("[*]    %s protocol '%s' not supported (error: '%s')." % (msg_prefix, protocol_to_test, e), "cyan", attrs=[]))
        except Exception as error:
            print(colored("[!] Probing failed: %s" % error, "red", attrs=[]))

    def do_probe_request_connection_limit(self, line):
        """
        Probe the WS server in order to determine the maximum number of connection allowed from a client.

        Note: Perform a initial connection using the "connect" command before to use this command in order to allow
        this command to know the connection context to use.
        """
        try:
            # Check if connection context is defined
            if self.__client_connection_parameters is None:
                print(colored("[!] Perform a initial connection using the 'connect' command !", "yellow", attrs=[]))
            else:
                # Define parser for command line arguments stored in the connection context (same like for "connect" command)
                parser = argparse.ArgumentParser()
                parser.add_argument('-t', action="store", dest="endpoint")
                parser.add_argument('-o', action="store", dest="origin", default=None)
                parser.add_argument('-e', action="store", dest="extra_http_headers", default=None)
                parser.add_argument('-p', action="store", dest="subprotocols", default=None)
                # Parse command line stored in the connection context (same like for "connect" command)
                args = parser.parse_args(self.__client_connection_parameters.split(" "))
                # Build custom headers map
                extra_headers = {}
                if args.extra_http_headers is not None:
                    for pair in args.extra_http_headers.split("§"):
                        parts = pair.split("=")
                        extra_headers[parts[0]] = parts[1]
                # Build subprotocols list
                subprotocols_set = []
                if args.subprotocols is not None:
                    for subprotocol in args.subprotocols.split("§"):
                        subprotocols_set.append(subprotocol)
                # Perform probing using connection context for each new connection
                print(colored("[*] Perform probing using connection context for each new connection...", "cyan", attrs=[]))
                max_connection = 0
                max_probing_limit = 1000000000
                connections_references_list = []
                for idx in range(1, max_probing_limit):
                    try:
                        if idx % 10 == 0:
                            print(colored("[*]    %s connections reached, continue probing..." % idx, "cyan", attrs=[]))
                        # Create a new connection using the connection context and hold the connection object via the list
                        # in order to avoid that the websocket client API release the connection to server
                        connections_references_list.append(create_connection(url=args.endpoint, timeout=10, header=extra_headers, origin=args.origin, subprotocols=subprotocols_set))
                    except (WebSocketException, IOError):
                        # Remove 1 to have the previous iteration step idx value
                        max_connection = idx - 1
                        # Exit because we have reach the limit
                        break
                if max_connection > 0:
                    print(colored("[*] Maximum connections limit identified to %s connections." % max_connection, "cyan", attrs=[]))
                else:
                    print(colored("[!] Maximum connections limit NOT identified BUT is superior to %s connections." % max_probing_limit, "yellow", attrs=[]))
                # Release connection to free the server and avoid DOS
                print(colored("[*] Release connections to free the server and avoid DOS...", "cyan", attrs=[]))
                connection_not_released_count = 0
                for connection in connections_references_list:
                    try:
                        connection.close()
                    except IOError:
                        connection_not_released_count += 1
                print(colored("[*] Connections released (%s connections released | %s connections not released due to error)." % (len(connections_references_list) - connection_not_released_count, connection_not_released_count), "cyan", attrs=[]))
        except Exception as error:
            print(colored("[!] Probing failed: %s" % error, "red", attrs=[]))

    def do_disconnect(self, line):
        """
        Close the current WS connection (no parameter required)
        """
        if self.__client is not None:
            try:
                self.__client.close()
                self.__client = None
                print(colored("[*] Connection closed.", "cyan", attrs=[]))
            except Exception as error:
                print(colored("[!] Close connection failed: %s" % error, "red", attrs=[]))

    def do_quit(self, line):
        """
        Exit the shell (no parameter required)
        """
        self.do_disconnect(line)
        return True

    def __store_exchanges_to_file(self, filename):
        """
        Save the exchange internal store dict to a JSON pretty printed string in a text file

        :param filename: Destination file
        """
        formatted_data = json.dumps(self.__exchanges, sort_keys=True, indent=2)
        with open(filename, "w") as ex_file:
            ex_file.write(formatted_data)

    def __store_data_to_file(self, data, filename):
        """
        Save the provided data to a JSON pretty printed string in a text file

        :param data: Data to save
        :param filename: Destination file
        """
        formatted_data = json.dumps(data, sort_keys=True, indent=2)
        with open(filename, "w") as ex_file:
            ex_file.write(formatted_data)

    def __send_messages(self, messages_list):
        """
        Send a list of messages and store associated exchanges for later processing

        :param messages_list: List of messages
        """
        error_count = 0
        idx = 0
        repetition_count = len(messages_list)
        print(colored("[*] Sending messages (Exchange = Request + Response)...", "cyan", attrs=[]))
        for msg in messages_list:
            start = time.clock()
            try:
                self.__check_connection_availability()
                self.__client.send(msg)
                response = self.__client.recv()
                self.__exchanges[idx] = {"REQUEST": msg, "RESPONSE": response, "IS_ERROR": False}
                print(colored("[*]    Exchange %03d successful." % idx, "cyan", attrs=[]))
            except Exception as err:
                self.__exchanges[idx] = {"REQUEST": msg, "RESPONSE": str(err), "IS_ERROR": True}
                error_count += 1
                print(colored("[!]    Exchange %03d meet error: %s" % (idx, err), "yellow", attrs=[]))
            self.__exchanges[idx]["RESPONSE_TIME"] = round(time.clock() - start, 2)
            self.__exchanges[idx]["REQUEST_LENGTH"] = len(self.__exchanges[idx]["REQUEST"])
            self.__exchanges[idx]["RESPONSE_LENGTH"] = len(self.__exchanges[idx]["RESPONSE"])
            idx += 1
        print(colored("[*] %s messages sent (%s errors | %s success)." % (repetition_count, error_count, (repetition_count - error_count)), "cyan", attrs=[]))

    def __check_connection_availability(self):
        """
        Check if connection is still opened, if not, reopen it automatically
        """
        print(colored("[*] Check if connection is still opened, if not, reopen it automatically...", "cyan", attrs=[]))
        if self.__client is None:
            self.do_connect(self.__client_connection_parameters)
        else:
            try:
                self.__client.send("TestAliveState")
                if self.__client.recv() is not None:
                    print(colored("[*] Connection is available.", "cyan", attrs=[]))
            except WebSocketConnectionClosedException as wse:
                self.do_connect(self.__client_connection_parameters)

    def __build_fuzzing_dicts(self, payload_files_list, payload_combinations, current_position):
        """
        Build a list of dict containing all payloads combinations coming from all payloads files

        :param payload_files_list: List of payloads files path
        :param payload_combinations: List of dict with all all payloads combinations (used for recursion)
        :param current_position: Current position in the placeholder collection according to the list of payloads files (used for recursion)
        :return: A list of dict containing all payloads combinations with their associated placeholders
        """
        if len(payload_files_list) > 0:
            current_payload_file = payload_files_list.pop(0)
            with open(current_payload_file) as payload_file:
                payloads = payload_file.readlines()
            payload_placeholder_key = "payload_%s" % current_position
            if len(payload_combinations) == 0:
                for payload in payloads:
                    payload_combinations.append({payload_placeholder_key: payload.rstrip('\n')})
                return self.__build_fuzzing_dicts(payload_files_list, payload_combinations, current_position + 1)
            else:
                new_payload_combinations = []
                for payload_combination in payload_combinations:
                    for payload in payloads:
                        new_dict = {payload_placeholder_key: payload.rstrip('\n')}
                        new_dict.update(payload_combination)
                        new_payload_combinations.append(new_dict)
                return self.__build_fuzzing_dicts(payload_files_list, new_payload_combinations, current_position + 1)
        else:
            return payload_combinations


if __name__ == "__main__":
    version = "1.0.0.dev"
    intro = ".:Welcome to the WebSocket probing shell:.\n\nVersion %s\n\nType help or ? to list commands.\n" % version
    colorama.init()
    WSProbingShell().cmdloop(intro)
