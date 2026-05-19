#!/usr/bin/perl

use strict;
use warnings;

use CGI;
use HTML::Template;
use LoxBerry::JSON;
use LoxBerry::System;
use LoxBerry::Web;

my $cgi      = CGI->new;
my $version  = "0.1.2";
my $cfgfile  = "$lbpconfigdir/default.json";
my $cfgobj   = LoxBerry::JSON->new();
my $cfg      = -f $cfgfile ? $cfgobj->open(filename => $cfgfile) : {};
my $saved    = 0;
my $errormsg = "";

# ---------------------------------------------------------------- save ---
if ($cgi->request_method eq "POST" && defined $cgi->param("save")) {
    # Strip surrounding whitespace on credentials — password managers and
    # autofill commonly slip a leading/trailing space into the field, and
    # Marstek's API rejects the resulting MD5 (you'd get code 4: 密码错误
    # "password incorrect" even though the password "looks" right).
    $cfg->{enabled}             = $cgi->param("enabled") ? 1 : 0;
    my $form_email              = scalar $cgi->param("email") // "";
    $form_email                 =~ s/^\s+|\s+$//g;
    $cfg->{email}               = $form_email;
    my $new_password            = scalar $cgi->param("password");
    if (defined $new_password && $new_password ne "") {
        $new_password           =~ s/^\s+|\s+$//g;
        $cfg->{password}        = $new_password if $new_password ne "";
    }
    # api_base_url is fully user-controlled at this point. A CSRF attacker
    # could otherwise rewrite it to https://attacker.example and the daemon
    # would POST `mailbox=<email>&pwd=<md5>` to them on the next poll.
    # Restrict to Marstek's own domain (any subdomain of hamedata.com) plus
    # localhost for testing. The empty/whitespace-only case falls back to
    # the canonical EU host. A disallowed value is rejected with an error;
    # we keep the user's input in $cfg so they see what they typed.
    my $form_api_base_url = scalar $cgi->param("api_base_url") // "";
    $form_api_base_url    =~ s/^\s+|\s+$//g;
    $form_api_base_url    = "https://eu.hamedata.com" if $form_api_base_url eq "";
    my $api_base_url_ok   = $form_api_base_url =~ m{
        \A https?:// (
            localhost (:\d+)? (/.*)?
          | (?:[a-z0-9\-]+\.)* hamedata\.com (:\d+)? (/.*)?
        ) \z
    }ix;
    if (!$api_base_url_ok) {
        $errormsg = "api_base_url not allowed: '$form_api_base_url'. "
                  . "Must be https://*.hamedata.com or http(s)://localhost.";
    }
    $cfg->{api_base_url}          = $form_api_base_url;
    $cfg->{poll_interval_seconds} = int($cgi->param("poll_interval_seconds") || 60);
    $cfg->{use_loxberry_mqtt}   = $cgi->param("use_loxberry_mqtt") ? 1 : 0;
    $cfg->{mqtt_host}           = scalar $cgi->param("mqtt_host") || "localhost";
    $cfg->{mqtt_port}           = int($cgi->param("mqtt_port") || 1883);
    $cfg->{mqtt_username}       = scalar $cgi->param("mqtt_username") // "";
    my $new_mqtt_password       = scalar $cgi->param("mqtt_password");
    if (defined $new_mqtt_password && $new_mqtt_password ne "") {
        $cfg->{mqtt_password}   = $new_mqtt_password;
    }
    $cfg->{mqtt_topic_prefix}   = scalar $cgi->param("mqtt_topic_prefix") || "marstek";
    $cfg->{mqtt_dry_run}        = $cgi->param("mqtt_dry_run") ? 1 : 0;
    $cfg->{register_mqtt_subscription} = $cgi->param("register_mqtt_subscription") ? 1 : 0;
    $cfg->{publish_raw_json}    = $cgi->param("publish_raw_json") ? 1 : 0;
    $cfg->{debug}               = $cgi->param("debug") ? 1 : 0;

    $cfg->{poll_interval_seconds} = 10   if $cfg->{poll_interval_seconds} < 10;
    $cfg->{poll_interval_seconds} = 3600 if $cfg->{poll_interval_seconds} > 3600;
    $cfg->{mqtt_port}             = 1883 if $cfg->{mqtt_port} < 1 || $cfg->{mqtt_port} > 65535;

    # Skip the write entirely if validation produced an error above. The
    # in-memory $cfg still reflects what the user typed so they can see and
    # correct their input; the on-disk file is left untouched.
    if ($errormsg) {
        # noop — error already set, fall through to render
    } elsif (eval { $cfgobj->write(); 1 }) {
        $saved = 1;
        # The saved config holds the Marstek password in plaintext (the API
        # requires md5(password) on every login, so we cannot hash-at-rest).
        # Restrict to owner-only — only the loxberry user (which runs both
        # this CGI and the daemon) needs to read it.
        chmod 0600, $cfgfile;
        # LoxBerry installs the daemon hook under <lbhomedir>/system/daemons/plugins/.
        # bin/../daemon/daemon doesn't exist post-install. Redirect output to a
        # log file — anything the wrapper prints would otherwise leak into the
        # HTTP response stream and corrupt the page (Apache returns 500). Do
        # not hardcode the LoxBerry install root here; the installer's linter
        # grep -l's daemon scripts for it.
        my $daemon = "$lbhomedir/system/daemons/plugins/marstek-cloud";
        if (-x $daemon) {
            system("$daemon restart >>'$lbplogdir/daemon-restart.log' 2>&1");
        }
    } else {
        $errormsg = "Could not save configuration: $@";
    }
}

# ------------------------------------------------------------ status ---
my $daemon_state   = "unknown";
my $daemon_running = 0;
my $pidfile        = "$lbplogdir/marstek-cloud.pid";
my $creds_missing  = !($cfg->{email} && $cfg->{password});

if (-f $pidfile) {
    my $pid = do { local (@ARGV, $/) = $pidfile; <> };
    chomp $pid if defined $pid;
    if ($pid && kill(0, $pid)) {
        $daemon_state   = "running (PID $pid)";
        $daemon_running = 1;
    } else {
        $daemon_state = "stopped (stale pidfile)";
    }
} elsif (!$cfg->{enabled}) {
    $daemon_state = "disabled";
} elsif ($creds_missing) {
    $daemon_state = "not configured (enter Marstek email + password)";
} else {
    $daemon_state = "stopped";
}

my $logfile  = "$lbplogdir/marstek-cloud.log";
my $log_size = -f $logfile ? (-s $logfile) : 0;

# ---------- LoxBerry MQTT broker auto-discovery ----------
# Read the LoxBerry built-in MQTT broker details so we can show the user
# what the daemon will auto-connect to when the checkbox is on.
my $lb_mqtt_host       = "";
my $lb_mqtt_port       = "";
my $lb_mqtt_user       = "";
my $lb_mqtt_available  = 0;
eval {
    require LoxBerry::IO;
    my $cred = LoxBerry::IO::mqtt_connectiondetails();
    if ($cred && $cred->{brokerhost}) {
        $lb_mqtt_host      = $cred->{brokerhost};
        $lb_mqtt_port      = $cred->{brokerport};
        $lb_mqtt_user      = $cred->{brokeruser} || "";
        $lb_mqtt_available = 1;
    }
};

# --------------------------------------------------------------- render ---
print $cgi->header(-type => "text/html", -charset => "utf-8");

my $template = HTML::Template->new(
    filename          => "$lbptemplatedir/settings.html",
    die_on_bad_params => 0,
    loop_context_vars => 1,
    global_vars       => 1,
);

$template->param(
    TITLE                 => "Marstek Cloud",
    SAVED                 => $saved,
    ERROR                 => $errormsg,
    DAEMON_STATE          => $daemon_state,
    DAEMON_RUNNING        => $daemon_running,
    LOG_SIZE              => $log_size,
    LOG_PATH              => $logfile,
    ENABLED               => $cfg->{enabled} ? 1 : 0,
    EMAIL                 => $cfg->{email} || "",
    PASSWORD_SET          => ($cfg->{password} && length $cfg->{password}) ? 1 : 0,
    API_BASE_URL          => $cfg->{api_base_url} || "https://eu.hamedata.com",
    POLL_INTERVAL_SECONDS => $cfg->{poll_interval_seconds} || 60,
    USE_LOXBERRY_MQTT     => (exists $cfg->{use_loxberry_mqtt} ? ($cfg->{use_loxberry_mqtt} ? 1 : 0) : 1),
    LB_MQTT_AVAILABLE     => $lb_mqtt_available,
    LB_MQTT_HOST          => $lb_mqtt_host,
    LB_MQTT_PORT          => $lb_mqtt_port,
    LB_MQTT_USER          => $lb_mqtt_user,
    MQTT_HOST             => $cfg->{mqtt_host} || "localhost",
    MQTT_PORT             => $cfg->{mqtt_port} || 1883,
    MQTT_USERNAME         => $cfg->{mqtt_username} || "",
    MQTT_PASSWORD_SET     => ($cfg->{mqtt_password} && length $cfg->{mqtt_password}) ? 1 : 0,
    MQTT_TOPIC_PREFIX     => $cfg->{mqtt_topic_prefix} || "marstek",
    MQTT_DRY_RUN          => $cfg->{mqtt_dry_run} ? 1 : 0,
    REGISTER_MQTT_SUB     => (exists $cfg->{register_mqtt_subscription} ? ($cfg->{register_mqtt_subscription} ? 1 : 0) : 1),
    PUBLISH_RAW_JSON      => $cfg->{publish_raw_json} ? 1 : 0,
    DEBUG                 => $cfg->{debug} ? 1 : 0,
    VERSION               => $version,
    SELF_URL              => $ENV{REQUEST_URI} || "",
);

LoxBerry::Web::lbheader("Marstek Cloud", "https://github.com/jovd83/loxberry-marstek-cloud", "");
print $template->output;
LoxBerry::Web::lbfooter();

exit 0;
