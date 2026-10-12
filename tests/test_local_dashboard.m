% Run with: matlab -batch "run('tests/test_local_dashboard.m')"
% Test the actual local CSV path, with no ThingSpeak key or toolbox.
testProject = fileparts(fileparts(mfilename('fullpath')));
testDir = tempname;
mkdir(testDir);
testCleanup = onCleanup(@() rmdir(testDir, 's'));
set(groot, 'defaultFigureVisible', 'off');
testVisibilityCleanup = onCleanup(@() set(groot, 'defaultFigureVisible', 'on'));
testColumns = {'temperature_c', 'humidity_pct', 'pressure_hpa', 'pm1_0', ...
    'pm2_5', 'pm10', 'oxidising_index', 'reducing_index'};
testStamps = ["2026-10-09 09:00:20+00:00"; "2026-10-09 09:00:00.366145+00:00"; ...
    "2026-10-09 09:00:40.742104+00:00"; "2026-10-09 09:00:20+00:00"];
testValues = repmat((1:4)', 1, 8);
testValues(1, 4) = -1;
testValues(3, 8) = Inf;
testInput = array2table(testValues, 'VariableNames', testColumns);
testInput.timestamp_utc = testStamps;
testInput.timestamp_sast = replace(replace(testStamps, '09:', '11:'), '+00:00', '+02:00');
% Match Python's output: the first two columns are unquoted timestamps.
testInput = movevars(testInput, {'timestamp_sast', 'timestamp_utc'}, 'Before', 1);
analysisCSV = fullfile(testDir, 'experiment.csv');
writetable(testInput, analysisCSV, 'QuoteStrings', false);
readAPIKey = ''; % Local analysis must bypass the cloud key check entirely.
run(fullfile(testProject, 'MATLAB_Visualization.m'));
assert(height(readings) == 3 && size(data, 1) == 3);
assert(strcmp(time.TimeZone, 'Africa/Johannesburg'));
assert(time(1) == datetime('2026-10-09 11:00:00.366145', ...
    'InputFormat', 'yyyy-MM-dd HH:mm:ss.SSSSSS', 'TimeZone', 'Africa/Johannesburg'));
assert(time(end) == datetime('2026-10-09 11:00:40.742104', ...
    'InputFormat', 'yyyy-MM-dd HH:mm:ss.SSSSSS', 'TimeZone', 'Africa/Johannesburg'));
assert(isnan(data(2, 4)) && isnan(data(3, 8)));
assert(isequal(readings.timestamp_sast, time));
assert(numel(findall(gcf, 'Type', 'axes')) == 8);
fprintf('PASS: local CSV, sorting, duplicates, microseconds, SAST, missing data and eight plots.\n');
close(gcf);

testInput = table("bad", 'VariableNames', {'timestamp_utc'});
writetable(testInput, analysisCSV);
testRejected = false;
try
    run(fullfile(testProject, 'MATLAB_Visualization.m'));
catch testError
    testRejected = contains(testError.message, 'produced by analyse.py');
end
assert(testRejected, 'Raw/unrecognised CSVs should direct the user to Python analysis first.');
fprintf('PASS: unrecognised input rejected before plotting or cloud access.\n');
