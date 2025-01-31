import os
import urllib
import pickle as pickle
import numpy as np
import matplotlib.pylab as plt
from scipy.interpolate import interp1d
from astropy.io import fits
from astropy.constants import c,h, k_B, G, M_sun, au, pc, u
from astropy.table import Table
from astropy import units as un
from astropy.convolution import Gaussian1DKernel, convolve_fft
import pandas as pd

from slabspec.helpers import fwhm_to_sigma, sigma_to_fwhm, markgauss, compute_thermal_velocity, get_molecule_identifier, extract_hitran_data,get_global_identifier

def spec_convol(wave,flux,dv):
    '''
    Convolve a spectrum, given wavelength in microns and flux density, by a given resolving power

    Parameters
    ---------
    wave : numpy array
        wavelength values, in microns
    flux : numpy array
        flux density values, in units of Energy/area/time/Hz
    dv : float
        Resolving power in km/s

    Returns
    --------
    newflux : numpy array
        Convolved spectrum flux density values, in same units as input

    '''
    R = c.value/(dv*1e3) #input dv in km/s, convert to m/s
    # find the minimum spacing between wavelengths in the dataset
    dws = np.abs(wave - np.roll(wave, 1))
    dw_min = np.min(dws)   #Minimum delta-wavelength between points in dataset

    fwhm = wave / R  # FWHM of resolution element as a function of wavelength ("delta lambda" in same units as wave)
    #fwhm / dw_min gives FWHM values expressed in units of minimum spacing, or the sampling for each wavelength
    #(sampling is sort of the number of data points per FWHM)
    #The sampling is different for each point in the wavelength array, because the FWHM is wavelength dependent
    #fwhm_s then gives the minimum value of the sampling - the most poorly sampled wavelength.
    fwhm_s = np.min(fwhm / dw_min)  # find mininumvalue of sampling for this dataset
    # but do not allow the sampling FWHM to be less than Nyquist
    # (i.e., make sure there are at least two points per resolution element)
    fwhm_s = np.max([2., fwhm_s])  #Will return 2 only if fwhm_s is less than 2
    #If you want all wavelengths to have the same sampling per resolution element,
    #then this ds gives the wavelength spacing for each wavelength (in units of wavelength)
    ds = fwhm / fwhm_s

    wave_constfwhm = np.cumsum(ds)+np.min(wave)

    '''
    # use the min wavelength as a starting point
    w = np.min(wave)

    #Initialize array to hold new wavelength values
    #Note: it's much faster (~50%) to append to lists than np.array()'s
    wave_constfwhm = []

    # doing this as a loop is slow, but straightforward.
    while w < np.max(wave):
        # use interpolation to get delta-wavelength from the sampling as a function of wavelength.
        # this method is over 5x faster than the original use of scipy.interpolate.interp1d.
        w += np.interp(w,wave,ds)  #Get value of ds at w, then add to old value of w
        wave_constfwhm.append(w)
        print(w)

    wave_constfwhm.pop()  # remove last point which is an extrapolation
    wave_constfwhm = np.array(wave_constfwhm)  #Convert list to numpy array
    '''

    # interpolate the flux onto the new wavelength set
    flux_constfwhm = np.interp(wave_constfwhm,wave,flux)

    # convolve the flux with a gaussian kernel; first convert the FWHM to sigma
    sigma_s = fwhm_s / 2.3548
    try:
        # for astropy < 0.4
        g = Gaussian1DKernel(width=sigma_s)
    except TypeError:
        # for astropy >= 0.4
        g = Gaussian1DKernel(sigma_s)
    # use boundary='extend' to set values outside the array to nearest array value.
    # this is the best approximation in this case.
    flux_conv = convolve_fft(flux_constfwhm, g, normalize_kernel=True, boundary='fill')
    flux_oldsampling = np.interp(wave, wave_constfwhm, flux_conv)

    return flux_oldsampling


def spec_convol_colette(wave, flux, dv):
    '''
    Convolve a spectrum, given wavelength in microns and flux density, by a given FWHM in velocity

    Parameters
    ---------
    wave : numpy array
        wavelength values, in microns
    flux : numpy array
        flux density values, in units of Energy/area/time/Hz
    dv : float
        FWHM of convolution kernel, in km/s

    Returns
    --------
    newflux : numpy array
        Convolved spectrum flux density values, in same units as input

    '''

    #Program assumes units of dv are km/s, and dv=FWHM
    dv = fwhm_to_sigma(dv)
    n = round(4.*dv/(c.value*1e-3)*np.median(wave)/(wave[1]-wave[0]))
    if (n < 10):
        n = 10.

    #Pad arrays to deal with edges
    dwave = wave[1]-wave[0]
    wave_low = np.arange(wave[0]-dwave*n, wave[0]-dwave, dwave)
    wave_high = np.arange(np.max(wave)+dwave, np.max(wave)+dwave*(n-1.), dwave)
    nlow = np.size(wave_low)
    nhigh = np.size(wave_high)
    flux_low = np.zeros(nlow)
    flux_high = np.zeros(nhigh)
    mask_low = np.zeros(nlow)
    mask_high = np.zeros(nhigh)
    mask_middle = np.ones(np.size(wave))
    wave = np.concatenate([wave_low, wave, wave_high])
    flux = np.concatenate([flux_low, flux, flux_high])
    mask = np.concatenate([mask_low, mask_middle, mask_high])

    newflux = np.copy(flux)

    if( n > (np.size(wave)-n)):
        print("Your wavelength range is too small for your kernel")
        print("Program will return an empty array")

    for i in np.arange(n, np.size(wave)-n+1):
        lwave = wave[np.int(i-n):np.int(i+n+1)]
        lflux = flux[np.int(i-n):np.int(i+n+1)]
        lvel = (lwave-wave[np.int(i)])/wave[np.int(i)]*c.value*1e-3
        nvel = (np.max(lvel)-np.min(lvel))/(dv*.2) +3
        vel = np.arange(nvel)
        vel = .2*dv*(vel-np.median(vel))
        kernel = markgauss(vel,mean=0,sigma=dv,area=1.)
        wkernel = np.interp(lvel,vel,kernel)   #numpy interp is almost factor of 2 faster than interp1d
        wkernel = wkernel/np.nansum(wkernel)
        newflux[np.int(i)] = np.nansum(lflux*wkernel)/np.nansum(wkernel[np.isfinite(lflux)])
        #Note: denominator is necessary to correctly account for NaN'd regions

    #Remove NaN'd regions
    nanbool = np.invert(np.isfinite(flux))   #Places where flux is not finite
    newflux[nanbool] = 'NaN'

    #Now remove padding
    newflux = newflux[mask==1]

    return newflux

#------------------------------------------------------------------------------------
def make_spec(molecule_name, n_col, temp, area, wmax=40, wmin=1, deltav=None, isotopologue_number=1, d_pc=1,
              aupmin=None, convol_fwhm=None, eupmax=None, vup=None, swmin=None):

    '''
    Create an IR spectrum for a slab model with given temperature, area, and column density

    Parameters
    ---------
    molecule_name : string
        String identifier for molecule, for example, 'CO', or 'H2O'
    n_col : float
        Column density, in m^-2
    temp : float
        Temperature of slab model, in K
    area : float
        Area of slab model, in m^2
    wmin : float, optional
        Minimum wavelength of output spectrum, in microns. Defaults to 1 micron.
    wmax : float, optional
        Maximum wavelength of output spectrum, in microns.  Defaults to 40 microns.
    deltav : float, optional
        sigma of local velocity distribution, in m/s.  Note this is NOT the global velocity distribution.
        Defaults to thermal speed of molecule given input temperature.
    isotopologue_number : float, optional
        Number representing isotopologue (1=most common, 2=next most common, etc.)
    d_pc : float, optional
        Distance to slab, in units of pc, for computing observed flux density.  Defaults to 1 pc.
    aupmin : float, optional
        Minimum Einstein-A coefficient for transitions
    swmin : float, optional
        Minimum line strength for transitions
    convol_fwhm : float, optional
        FWHM of convolution kernel, in km/s.
    res : float, optional
        max resolution of spectrum, in microns.  Must be significantly higher than observed spectrum for correct calculation.
        Defaults to 1e-4.
    eupmax : float, optional
        Maximum energy of transitions to consider, in K
    vup : float, optional
        Optional parameter to restrict output to certain upper level vibrational states

    Returns
    --------
    slabdict : dictionary
        Dictionary includes two astropy tables:
          lineparams : line parameters from HITRAN, integrated line fluxes, peak tau
          spectrum : wavelength, flux, convolflux, tau
        and two dictionaries
          lines : wave_arr (in microns), flux_arr (in mks), velocity (in km/s) - for plotting individual lines
          modelparams : model parameters: Area, column density, temperature, local velocity, convolution fwhm
    '''
    oversamp = 3
    isot = isotopologue_number
    si2jy = 1e26   #SI to Jy flux conversion factor

    #If local velocity field is not given, assume sigma given by thermal velocity
    if(deltav is None):
        deltav = compute_thermal_velocity(molecule_name, temp)

    #Internal resolving power needed to resolve deltav
    res = oversamp*c.value/deltav

    #Read HITRAN data
    hitran_data = extract_hitran_data(molecule_name,wmin,wmax,isotopologue_number=isotopologue_number, eupmax=eupmax, aupmin=aupmin, swmin=swmin)

    #Select for desired vup if relevant
    if(vup is not None):
        try:
            x = int(hitran_data['Vp'][0])
        except ValueError:
            print("Vp is not an integer, so the vup parameter cannot be used.  Ignoring this parameter.")
            vup = None
    if(vup is not None):
        vupbool = [(int(myvp)==1) for myvp in hitran_data['Vp']]
        hitran_data = hitran_data[vupbool]


    wn0 = hitran_data['wn']*1e2 # now m-1
    aup = hitran_data['a']
    eup = (hitran_data['elower']+hitran_data['wn'])*1e2 #now m-1
    gup = hitran_data['gp']

    #Compute partition function
    q = compute_partition_function(molecule_name,temp,isot)

    #Begin calculations
    afactor = ((aup*gup*n_col)/(q*8.*np.pi*(wn0)**3.)) #mks
    efactor = h.value*c.value*eup/(k_B.value*temp)
    wnfactor = h.value*c.value*wn0/(k_B.value*temp)
    phia = 1./(deltav*np.sqrt(2.0*np.pi))
    efactor2 = hitran_data['eup_k']/temp
    efactor1 = hitran_data['elower']*1.e2*h.value*c.value/k_B.value/temp
    tau0 = afactor*(np.exp(-1.*efactor1)-np.exp(-1.*efactor2))*phia  #Avoids numerical issues at low T

    dvel = deltav/oversamp    #m/s
    nvel = 10*oversamp+1 #5 sigma window
    vel = (dvel*(np.arange(0,nvel)-(nvel-1)/2))

    omega = area/(d_pc*pc.value)**2.
    fthin = aup*gup*n_col*h.value*c.value*wn0/(q*4.*np.pi)*np.exp(-efactor)*omega # Energy/area/time, mks

    #Loop over transitions and velocities to calculate flux
    nlines = np.size(tau0)
    tau = np.zeros([nlines,nvel])
    wave = np.zeros([nlines,nvel])
    for ha,mytau in enumerate(tau0):
        tau[ha,:] = tau0[ha]*np.exp(-vel**2./(2.*deltav**2.))
        wave[ha,:] = 1.e6/wn0[ha]*(1+vel/c.value)

    #Interpolate over wavelength space so that all lines can be added together
    w_arr = wave            #nlines x nvel
    f_arr = w_arr-w_arr     #nlines x nvel
    nbins = int(oversamp*wmax/(wmax-wmin)*(c.value/deltav))

    #Create arrays to hold full spectrum (optical depth vs. wavelength)
    totalwave = np.logspace(np.log10(wmin),np.log10(wmax),nbins)
    totaltau = np.zeros(nbins)

    #Create array to hold line fluxes (one flux value per line)
    lineflux = np.zeros(nlines)
    totalwave_index = np.arange(totalwave.size)
    index_interp = interp1d(totalwave,totalwave_index)
    for i in range(nlines):

        minw = np.min(wave[i,:])
        maxw = np.max(wave[i,:])
        minindex = int(index_interp(minw))
        maxindex = int(index_interp(maxw))

        w = np.arange(minindex,maxindex)

        if(w.size > 0):
            newtau = np.interp(totalwave[w],wave[i,:], tau[i,:])
            totaltau[w] += newtau
            f_arr[i,:] = 2*h.value*c.value*wn0[i]**3./(np.exp(wnfactor[i])-1.0e0)*(1-np.exp(-tau[i,:]))*omega
            lineflux[i] = np.sum(f_arr[i,:]) * (dvel/c.value) * (c.value*wn0[i]) #in W/m2

    wave_arr = wave
    wn = 1.e6/totalwave #m^{-1}
    wnfactor = h.value*c.value*wn/(k_B.value*temp)
    flux = 2*h.value*c.value*wn**3./(np.exp(wnfactor)-1.0e0)*(1-np.exp(-totaltau))*si2jy*omega #in Jy

    wave = totalwave

    #convol_fwhm should be set to FWHM of convolution kernel, in km/s
    convolflux = np.copy(flux)
    if(convol_fwhm is not None):
        convolflux = spec_convol(wave,flux,convol_fwhm)

    slabdict = {}

    #Line params
    hitran_data['lineflux'] = lineflux
    hitran_data['tau_peak'] = tau0
    hitran_data['fthin'] = fthin
    slabdict['lineparams'] = hitran_data

    #Line flux array
    lines = {'flux_arr':f_arr , 'wave_arr':wave_arr , 'velocity':vel*1e-3}
    slabdict['lines'] = lines

    #Spectrum
    spectrum_table = Table([wave, flux, convolflux, totaltau], names=('wave', 'flux', 'convolflux','totaltau'),  dtype=('f8', 'f8', 'f8','f8'))
    spectrum_table['wave'].unit = 'micron'
    spectrum_table['flux'].unit = 'Jy'
    spectrum_table['convolflux'].unit = 'Jy'
    slabdict['spectrum'] = spectrum_table

    #Model params
    if(convol_fwhm is not None):
        convol_fwhm = convol_fwhm*un.km/un.s
    modelparams_table = {'area':area*un.meter*un.meter,'temp':temp*un.K,'n_col':n_col/un.meter/un.meter, 'res':res*un.micron,
                         'deltav':deltav*un.meter/un.s, 'convol_fwhm':convol_fwhm, 'd_pc':d_pc*un.parsec,
                         'isotopologue_number':isot,'molecule_name':molecule_name}
    slabdict['modelparams'] = modelparams_table

    #Line-by-line data
    hitran_data['tau0'] = tau0
    hitran_data['lineflux'] = lineflux
    slabdict['moldata'] = hitran_data

    return slabdict

def write_slab(slabdict,filename='slabmodel.fits'):
    wave = slabdict['spectrum']['wave']
    flux = slabdict['spectrum']['convolflux']

    c1 = fits.Column(name='wave', array=wave, format='F')
    c2 = fits.Column(name='flux', array=flux, format='F')
    t1 = fits.BinTableHDU.from_columns([c1, c2])

    moldata = slabdict['moldata']
    mol_cols = []
    for key in moldata.keys():
        try:
            mol_cols.append(fits.Column(name=key,array=moldata[key],format='F'))
        except:
            mol_cols.append(fits.Column(name=key,array=moldata[key],format='A'))
    t2 = fits.BinTableHDU.from_columns(mol_cols)

    primary = fits.PrimaryHDU()
    hdulist = fits.HDUList([primary,t1,t2])

    hdulist.writeto(filename,overwrite=True)



def compute_partition_function(molecule_name,temp,isotopologue_number=1):
    '''
    For a given input molecule name, isotope number, and temperature, return the partition function Q

    Parameters
    ----------
    molecule_name : string
        The molecule name string (e.g., 'CO', 'H2O')
    temp : float
        The temperature at which to compute the partition function
    isotopologue_number : float, optional
        Isotopologue number, with 1 being most common, etc. Defaults to 1.

    Returns
    -------
    q : float
      The partition function
    '''

    G = get_global_identifier(molecule_name, isotopologue_number=isotopologue_number)
    qurl = 'https://hitran.org/data/Q/'+'q'+str(G)+'.txt'
    handle = urllib.request.urlopen(qurl)
    qdata = pd.read_csv(handle,sep=' ',skipinitialspace=True,names=['temp','q'],header=None)

#May want to add code with local file access
#    pathmod=os.path.dirname(__file__)
#    if not os.path.exists(qfilename):  #download data from internet
       #get https://hitran.org/data/Q/qstr(G).txt

    q = np.interp(temp,qdata['temp'],qdata['q'])
    return q


#Make this its own function
def make_rotation_diagram(lineparams):
    '''
    Take ouput of make_spec and use it to compute rotation diagram parameters.

    Parameters
    ---------
    lineparams: dictionary
        dictionary output from make_spec

    Returns
    --------
    rot_table: astropy Table
        Table of x and y values for rotation diagram.

    '''
    x = lineparams['eup_k']
    y = np.log(lineparams['lineflux']/(lineparams['wn']*lineparams['gp']*lineparams['a']))
    rot_table = Table([x, y], names=('x', 'y'),  dtype=('f8', 'f8'))
    rot_table['x'].unit = 'K'

    return rot_table
