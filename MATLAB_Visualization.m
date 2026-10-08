% Full-flight dashboard for ThingSpeak MATLAB Visualization or desktop MATLAB.
% Recovered entries carry their flight window; follow the most recent upload
% automatically unless explicit flightStartUTC/flightEndUTC are supplied.
% Older uploads can use the lines from upload/matlab_flight_window.txt.
% Supply readAPIKey privately in the visualization/workspace, or through the
% THINGSPEAK_READ_API_KEY environment variable on desktop MATLAB.
% Original light/raw gas/NH3 values are recovered by analyse.py from status;
% this dashboard retains the existing eight plots and field numbering.

if ~exist('readChannelID', 'var')
    readChannelID = 3429238;
end
if ~exist('readAPIKey', 'var')
    readAPIKey = getenv('THINGSPEAK_READ_API_KEY');
end
if isempty(readAPIKey)
    error('Set readAPIKey privately to the channel Read API key.');
end
if ~exist('autoFlightWindow', 'var')
    autoFlightWindow = ~exist('flightStartUTC', 'var') || ~exist('flightEndUTC', 'var');
end
if autoFlightWindow
    try
        latest = webread(sprintf('https://api.thingspeak.com/channels/%d/feeds.json', readChannelID), ...
            'api_key', readAPIKey, 'results', 1, 'status', 'true', weboptions('Timeout', 30));
        % A recovered older flight has newer entry IDs but older timestamps.
        % Read the last inserted entry, rather than the latest acquisition time.
        latest.feeds = webread(sprintf('https://api.thingspeak.com/channels/%d/feeds/%d.json', ...
            readChannelID, latest.channel.last_entry_id), 'api_key', readAPIKey, ...
            'status', 'true', weboptions('Timeout', 30));
    catch
        error('Cannot read the latest flight metadata. Check channel access and the Read API key.');
    end
    if isstruct(latest) && isfield(latest, 'feeds') && ~isempty(latest.feeds) ...
            && isfield(latest.feeds(end), 'status') && ~isempty(latest.feeds(end).status)
        try
            latestStatus = jsondecode(latest.feeds(end).status);
        catch
            latestStatus = struct();
        end
        if isstruct(latestStatus) && isfield(latestStatus, 'atmo') && latestStatus.atmo == 1 ...
                && isfield(latestStatus, 'fs') && isfield(latestStatus, 'fe') ...
                && isnumeric(latestStatus.fs) && isscalar(latestStatus.fs) && isfinite(latestStatus.fs) ...
                && isnumeric(latestStatus.fe) && isscalar(latestStatus.fe) && isfinite(latestStatus.fe) ...
                && latestStatus.fe >= latestStatus.fs
            startHint = datetime(latestStatus.fs, 'ConvertFrom', 'posixtime', 'TimeZone', 'UTC');
            endHint = datetime(latestStatus.fe + 1, 'ConvertFrom', 'posixtime', 'TimeZone', 'UTC');
            startHint.Format = 'yyyy-MM-dd HH:mm:ss';
            endHint.Format = 'yyyy-MM-dd HH:mm:ss';
            flightStartUTC = char(startHint);
            flightEndUTC = char(endHint);
        end
    end
end
if ~exist('flightStartUTC', 'var') || ~exist('flightEndUTC', 'var')
    error('No recovered flight window found. Upload a flight or set bounds from matlab_flight_window.txt.');
end
startUTC = datetime(flightStartUTC, 'InputFormat', 'yyyy-MM-dd HH:mm:ss', 'TimeZone', 'UTC');
endUTC = datetime(flightEndUTC, 'InputFormat', 'yyyy-MM-dd HH:mm:ss', 'TimeZone', 'UTC');
if isnat(startUTC) || isnat(endUTC) || endUTC <= startUTC
    error('The UTC flight window must have a valid start and a later end.');
end

% Read all fields together so every curve uses the same sample timestamps.
% Split any capped response; an 8000-point response may hide earlier samples.
windows = [startUTC, endUTC];
data = zeros(0, 8);
time = NaT(0, 1, 'TimeZone', 'UTC');
while ~isempty(windows)
    window = windows(end, :);
    windows(end, :) = [];
    [values, stamps] = thingSpeakRead(readChannelID, 'Fields', 1:8, ...
        'DateRange', window, 'OutputFormat', 'matrix', 'ReadKey', readAPIKey);
    if size(values, 1) >= 8000
        span = seconds(window(2) - window(1));
        if span < 2
            error('Too many entries in one second to read safely.');
        end
        midpoint = window(1) + seconds(floor(span / 2));
        windows = [windows; window(1), midpoint; midpoint, window(2)]; %#ok<AGROW>
        continue
    end
    if isempty(values)
        continue
    end
    if size(values, 2) ~= 8 || size(values, 1) ~= numel(stamps)
        error('Unexpected response: check that all eight channel fields are enabled.');
    end
    stamps.TimeZone = 'UTC';
    data = [data; values]; %#ok<AGROW>
    time = [time; stamps(:)]; %#ok<AGROW>
end
if isempty(time)
    error('No channel entries in the selected UTC flight window.');
end
% Inclusive split boundaries can repeat an entry. Sort by acquisition time,
% not entry ID: recovery inserts old data after newer channel entries.
[time, uniqueRows] = unique(time, 'sorted');
data = data(uniqueRows, :);
inFlight = time >= startUTC & time <= endUTC;
data = data(inFlight, :);
time = time(inFlight);
data(~isfinite(data)) = NaN;
indices = data(:, 4:8);
indices(indices < 0) = NaN;
data(:, 4:8) = indices;
time.TimeZone = 'Africa/Johannesburg';

titles = {'Temperature', 'Humidity', 'Pressure', 'PM1.0', ...
    'PM2.5', 'PM10', 'Oxidising Index', 'Reducing Index'};
units = {'deg C', '%', 'hPa', 'ug/m^3', 'ug/m^3', 'ug/m^3', '0-100', '0-100'};
for field = 1:8
    subplot(4, 2, field)
    plot(time, data(:, field))
    title(titles{field})
    ylabel(units{field})
    xlabel('Time (SAST)')
    xtickformat('dd-MMM HH:mm')
    if field >= 7
        ylim([0 100])
    end
    grid on
end
sgtitle(sprintf('Atmospheric flight: %d readings', numel(time)))
